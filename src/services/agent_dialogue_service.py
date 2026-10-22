"""Typed application service for the bounded AoiDots AgentDialogue contract.

The durable dialogue models contain references and authorization evidence.  A
normal encrypted ``ConversationMessage`` remains the only body store.  Run,
Work, outbox, sequence and lease transitions are delegated to the existing
``AgentRunService`` and ``AgentDialogueTransport`` boundaries.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import desc, inspect, select
from sqlalchemy.exc import OperationalError

from ..memory.models import (
    Agent,
    AgentDialogue,
    AgentDialogueBudgetRoot,
    AgentDialogueDelivery,
    AgentDialogueInput,
    AgentExecutionBinding,
    AgentPrivateRoot,
    AgentRevision,
    AgentRun,
    AgentTaskAssignment,
    AgentWorkItem,
    ConversationParticipant,
    ConversationMessage,
    ConversationSession,
    Task,
)
from .agent_dialogue_projection import build_turn_context, project_dialogue, project_profile
from .agent_dialogue_answer import (
    AgentDialogueAnswer,
    AgentDialogueAnswerError,
    AgentDialogueQuestionContract,
    build_agent_dialogue_question_contract,
    canonical_answer_digest,
    parse_agent_dialogue_answer,
    question_contract_from_message,
)
from .agent_dialogue_budget import (
    DialogueBudget,
    DialogueBudgetError,
    get_dialogue_budget_owner,
    normalize_dialogue_budgets,
)
from .agent_private_provisioning import AgentPrivateProvisioningService
from .agent_run_service import AgentRunService
from .conversation_agent_service import ConversationAgentService

if TYPE_CHECKING:
    from .agent_dialogue_transport import AgentDialogueTransport

logger = logging.getLogger(__name__)

_DISPATCH_BRIDGE_CACHE: dict[int, Any] = {}

_ACTIVE_RUN_STATES = frozenset({"queued", "running", "awaiting_approval", "uncertain"})
_TERMINAL_RUN_STATES = frozenset({"succeeded", "failed", "cancelled"})
_DELIVERY_KINDS = frozenset({"request", "response", "control", "event"})
# These are the only post-CAS answer outcomes that are safe to report as a
# successful durable answer.  The answer/Input rows are already committed;
# the provider-free continuation can be retried from the persisted outbox.
# Authorization, provenance, lease, and CAS failures must still surface.
_RECOVERABLE_HUMAN_ANSWER_DISPATCH_CODES = frozenset(
    {"dialogue_human_answer_deferred", "dialogue_handoff_deferred"}
)
_FORBIDDEN_BODY_KEYS = frozenset(
    {"body", "raw_body", "content", "message", "answer", "question", "prompt", "tool_args", "provider_args"}
)
_MAX_TITLE = 160
_MAX_INSTRUCTION = 30_000
_MAX_BUDGET = {
    "turns": 100,
    "max_turns": 100,
    "depth": 32,
    "max_depth": 32,
    "attempts": 16,
    "max_attempts": 16,
    "tokens": 1_000_000,
    "max_tokens": 1_000_000,
}


class AgentDialogueError(RuntimeError):
    code = "agent_dialogue_error"
    status_code = 409

    def __init__(self, message: str = "Agent dialogue operation failed", *, code: str | None = None):
        self.code = str(code or self.code)
        super().__init__(message)


class AgentDialogueNotFoundError(AgentDialogueError):
    code = "agent_dialogue_not_found"
    status_code = 404


class AgentDialogueAuthorizationError(AgentDialogueError):
    code = "agent_dialogue_access_denied"
    status_code = 403


class AgentDialogueConflictError(AgentDialogueError):
    code = "agent_dialogue_stale"
    status_code = 409


class AgentDialogueValidationError(AgentDialogueError):
    code = "agent_dialogue_invalid"
    status_code = 422


def get_agent_dialogue_dispatch_bridge(
    db_manager: Any | None = None,
    *,
    config: Any | None = None,
) -> Any:
    """Return the one ordinary AgentRunService bridge for this server scope."""

    manager = db_manager
    if manager is None:
        from ..memory.database import get_database_manager

        manager = get_database_manager()
    key = id(manager)
    bridge = _DISPATCH_BRIDGE_CACHE.get(key)
    if bridge is None:
        bridge = AgentRunService(manager, config=config)
        _DISPATCH_BRIDGE_CACHE[key] = bridge
    return bridge


def _id(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _uuid(value: Any, field_name: str, *, required: bool = True) -> UUID | None:
    if value in (None, ""):
        if required:
            raise AgentDialogueValidationError(f"{field_name} is required", code=f"{field_name}_required")
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise AgentDialogueValidationError(f"{field_name} is invalid", code=f"{field_name}_invalid") from exc


def _text(value: Any, *, limit: int = 512) -> str:
    return str(value or "").strip()[:limit]


def _now() -> datetime:
    return datetime.utcnow()


async def _maybe(value: Any) -> Any:
    import inspect

    return await value if inspect.isawaitable(value) else value


def _safe_refs(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep only typed references in authorization/delivery metadata."""

    raw = value if isinstance(value, Mapping) else {}
    result: dict[str, Any] = {}
    allowed = {
        "dialogue_id",
        "delivery_id",
        "source_message_id",
        "source_run_id",
        "source_work_item_id",
        "source_session_id",
        "receiver_session_id",
        "receiver_run_id",
        "receiver_work_item_id",
        "receiver_message_id",
        "transport_work_item_id",
        "root_session_id",
        "worker_session_id",
        "root_agent_id",
        "root_agent_revision_id",
        "worker_agent_id",
        "worker_agent_revision_id",
        "owner_user_id",
        "project_id",
        "task_id",
        "control_revision",
        "expected_control_revision",
        "provenance_hash",
        "input_id",
        "kind",
        "sequence",
        "depth",
    }
    for key in allowed:
        item = raw.get(key)
        if item in (None, ""):
            continue
        if isinstance(item, (str, int, bool)):
            result[key] = str(item) if key.endswith("_id") else item
    return result


def _bounded_json(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or len(value) > 32:
        raise AgentDialogueValidationError("metadata must be a bounded object", code="metadata_invalid")
    result: dict[str, Any] = {}
    for key, item in list(value.items())[:32]:
        name = _text(key, limit=96)
        lowered = name.casefold()
        if lowered in _FORBIDDEN_BODY_KEYS or any(marker in lowered for marker in ("secret", "token", "password", "credential", "authorization")):
            raise AgentDialogueValidationError("message bodies and secrets are not ledger metadata", code="body_metadata_forbidden")
        if isinstance(item, (str, int, bool)) or item is None:
            result[name] = _text(item, limit=512) if isinstance(item, str) else item
        elif isinstance(item, (list, tuple)):
            result[name] = [_text(entry, limit=256) for entry in list(item)[:32]]
        else:
            raise AgentDialogueValidationError("metadata values must be scalar", code="metadata_invalid")
    return result


_SOURCE_CAPABILITY_MISSING = object()


def _source_command_capabilities(
    value: Any,
    *,
    drop_unsupported: bool = False,
) -> list[str]:
    """Validate the bounded command-capability wire shape.

    The ordinary tool-policy sanitizer is authoritative for the capability
    catalog.  Server-built context may sanitize unsupported tool names away;
    an externally supplied anchor must already be sanitized and is rejected
    when it tries to smuggle a tool name or unknown capability into the pin.
    """

    if not isinstance(value, (list, tuple)) or len(value) > 32:
        raise AgentDialogueAuthorizationError(
            "source command capabilities must be a bounded list",
            code="source_manifest_invalid",
        )
    raw: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise AgentDialogueAuthorizationError(
                "source command capabilities must contain strings",
                code="source_manifest_invalid",
            )
        normalized = item.strip().casefold()
        if not normalized:
            raise AgentDialogueAuthorizationError(
                "source command capabilities contain an empty item",
                code="source_manifest_invalid",
            )
        raw.append(normalized)
    from ..llm.tool_policy import sanitize_command_capabilities

    sanitized = list(sanitize_command_capabilities(raw))
    if not drop_unsupported and sanitized != raw:
        raise AgentDialogueAuthorizationError(
            "source command capabilities are not sanitized",
            code="source_manifest_invalid",
        )
    return sanitized


def validate_source_human_anchor(
    anchor: Mapping[str, Any] | None,
    *,
    actor_user_id: Any,
    trigger_message: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate canonical human sender, command caps and exact pin hashes."""

    if not isinstance(anchor, Mapping):
        raise AgentDialogueAuthorizationError("canonical source human anchor is required", code="source_human_anchor_required")
    sender_type = _text(anchor.get("sender_type") or anchor.get("sender_kind"), limit=32).casefold()
    sender_id = _uuid(anchor.get("sender_id") or anchor.get("user_id"), "sender_id")
    actor = _uuid(actor_user_id, "actor_user_id")
    role = _text(anchor.get("message_role") or anchor.get("role"), limit=32).casefold()
    message_id = _uuid(anchor.get("trigger_message_id") or anchor.get("source_message_id") or anchor.get("message_id"), "trigger_message_id")
    if sender_type not in {"user", "human"} or sender_id != actor:
        raise AgentDialogueAuthorizationError("source is not the authenticated human", code="source_human_required")
    if role and role != "user":
        raise AgentDialogueAuthorizationError("source message is not a human message", code="source_message_invalid")
    if "command_capabilities" not in anchor:
        raise AgentDialogueAuthorizationError("source command capabilities are missing", code="source_manifest_invalid")
    raw_capabilities = anchor["command_capabilities"]
    capabilities = _source_command_capabilities(raw_capabilities)
    manifest_hash = _text(anchor.get("manifest_hash") or anchor.get("execution_manifest_hash"), limit=64).lower()
    binding_hash = _text(anchor.get("binding_hash") or anchor.get("agent_binding_hash"), limit=64).lower()
    if len(manifest_hash) != 64 or any(c not in "0123456789abcdef" for c in manifest_hash):
        raise AgentDialogueAuthorizationError("source execution manifest is not pinned", code="source_manifest_invalid")
    if len(binding_hash) != 64 or any(c not in "0123456789abcdef" for c in binding_hash):
        raise AgentDialogueAuthorizationError("source Agent binding is not pinned", code="source_binding_invalid")
    if trigger_message is not None:
        if (
            _uuid(trigger_message.get("id") or trigger_message.get("message_id"), "trigger_message_id") != message_id
            or _text(trigger_message.get("role"), limit=32).casefold() != "user"
            or _text(trigger_message.get("sender_type"), limit=32).casefold() not in {"user", "human"}
            or _uuid(trigger_message.get("sender_id"), "sender_id") != actor
        ):
            raise AgentDialogueAuthorizationError("source message is not canonical", code="source_message_invalid")
        if "command_capabilities" in trigger_message:
            trigger_capabilities = _source_command_capabilities(trigger_message["command_capabilities"])
            if trigger_capabilities != capabilities:
                raise AgentDialogueAuthorizationError(
                    "source command capabilities are not canonical",
                    code="source_manifest_invalid",
                )
    return {
        "trigger_message_id": str(message_id),
        "sender_type": "user",
        "sender_id": str(actor),
        "message_role": "user",
        "command_capabilities": capabilities,
        "manifest_hash": manifest_hash,
        "binding_hash": binding_hash,
        "source_run_id": _id(anchor.get("source_run_id") or anchor.get("human_run_id")),
        "source_work_item_id": _id(anchor.get("source_work_item_id") or anchor.get("human_work_item_id")),
    }


def validate_delivery(
    *,
    dialogue: AgentDialogue,
    kind: str,
    source_agent_run_id: Any,
    target_session_id: Any | None = None,
    control_revision: int,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate the body-free transport envelope before enqueue."""

    normalized_kind = _text(kind, limit=32).casefold()
    if normalized_kind not in _DELIVERY_KINDS:
        raise AgentDialogueValidationError("delivery kind is not allowlisted", code="delivery_kind_invalid")
    source_run = _uuid(source_agent_run_id, "source_run_id")
    if target_session_id is not None and _uuid(target_session_id, "target_session_id") != dialogue.worker_session_id:
        raise AgentDialogueAuthorizationError("delivery target is outside dialogue", code="delivery_target_invalid")
    expected = int(dialogue.control_revision or 0)
    if int(control_revision) != expected:
        raise AgentDialogueConflictError("dialogue control revision is stale", code="dialogue_control_stale")
    return {
        "kind": normalized_kind,
        "source_run_id": str(source_run),
        "target_session_id": str(dialogue.worker_session_id),
        "expected_control_revision": expected,
        "metadata": _bounded_json(metadata or {}),
    }


class AgentDialogueService:
    """Authorize and compose the typed AgentDialogue/transport operations."""

    def __init__(
        self,
        db_manager: Any | None = None,
        *,
        config: Any | None = None,
        transport: Any | None = None,
        dispatch_bridge: Any | None = None,
    ) -> None:
        self._db_manager = db_manager
        self.config = config
        self.transport = transport
        self.dispatch_bridge = dispatch_bridge

    def _manager(self) -> Any:
        if self._db_manager is not None:
            return self._db_manager
        from ..memory.database import get_database_manager

        return get_database_manager()

    async def _session(self) -> Any:
        manager = self._manager()
        value = manager.get_session()
        value = await _maybe(value)
        if value is None:
            raise AgentDialogueError("database is unavailable", code="database_unavailable")
        return value

    def _transport(self) -> Any:
        if self.transport is not None:
            return self.transport
        if self.dispatch_bridge is None:
            raise AgentDialogueError(
                "typed dialogue dispatch bridge is unavailable",
                code="dialogue_dispatch_bridge_unavailable",
            )
        try:
            from .agent_dialogue_transport import AgentDialogueTransport
        except ImportError as exc:
            raise AgentDialogueError("dialogue transport is unavailable", code="transport_unavailable") from exc
        return AgentDialogueTransport(
            self._manager(),
            dispatch_bridge=self.dispatch_bridge,
            config=self.config,
        )

    async def _load_dialogue(self, session: Any, dialogue_id: Any, *, lock: bool = False) -> AgentDialogue:
        dialogue_uuid = _uuid(dialogue_id, "dialogue_id")
        statement = select(AgentDialogue).where(AgentDialogue.id == dialogue_uuid)
        if lock:
            statement = statement.with_for_update()
        row = (await session.execute(statement)).scalars().first()
        if row is None:
            raise AgentDialogueNotFoundError()
        return row

    async def _load_input_question_contract(
        self,
        session: Any,
        dialogue: AgentDialogue,
        input_row: AgentDialogueInput,
    ) -> tuple[ConversationMessage, AgentDialogueQuestionContract]:
        """Load the authorized encrypted question body and its UI contract."""

        message_id = input_row.source_message_id or input_row.reference_id
        message_uuid = _uuid(message_id, "question_message_id", required=False)
        if message_uuid is None:
            raise AgentDialogueValidationError(
                "dialogue input question source is missing",
                code="dialogue_question_source_missing",
            )
        message = await session.get(ConversationMessage, message_uuid)
        if message is None or str(message.session_id) not in {
            str(dialogue.root_session_id),
            str(dialogue.worker_session_id),
        }:
            raise AgentDialogueAuthorizationError(
                "dialogue input question source is outside the dialogue",
                code="dialogue_question_source_invalid",
            )
        if (
            str(message.role or "").casefold() != "assistant"
            or str(message.sender_type or "").casefold() != "agent"
            or str(message.session_id) != str(dialogue.worker_session_id)
            or str(message.sender_id) != str(dialogue.worker_agent_id)
        ):
            raise AgentDialogueAuthorizationError(
                "dialogue input question source is not an Agent message",
                code="dialogue_question_source_invalid",
            )
        try:
            if not str(message.content or "").strip():
                raise AgentDialogueValidationError(
                    "dialogue input question body is empty",
                    code="dialogue_question_body_missing",
                )
        except AgentDialogueValidationError:
            raise
        except Exception as exc:
            raise AgentDialogueAuthorizationError(
                "dialogue input question body is unavailable",
                code="dialogue_question_source_invalid",
            ) from exc
        metadata = message.message_metadata if isinstance(message.message_metadata, Mapping) else {}
        source_run_id = _uuid(input_row.source_run_id, "source_run_id", required=False)
        if source_run_id is not None:
            source_run = await session.get(AgentRun, source_run_id)
            source_work_id = _uuid(input_row.source_work_item_id, "source_work_item_id", required=False)
            source_work = await session.get(AgentWorkItem, source_work_id) if source_work_id is not None else None
            if (
                source_run is None
                or str(source_run.session_id) != str(dialogue.worker_session_id)
                or str(source_run.agent_id) != str(dialogue.worker_agent_id)
                or str(source_run.agent_revision_id) != str(dialogue.worker_agent_revision_id)
                or str(source_run.work_item_id) != str(source_work_id)
                or source_work is None
                or str(source_work.conversation_session_id) != str(dialogue.worker_session_id)
                or str(source_work.assigned_agent_id) != str(dialogue.worker_agent_id)
                or str(source_work.agent_revision_id) != str(dialogue.worker_agent_revision_id)
                or str(source_work.active_agent_run_id or "") not in {"", str(source_run.id)}
                or str(metadata.get("agent_run_id") or "") != str(source_run.id)
            ):
                raise AgentDialogueAuthorizationError(
                    "dialogue input question source Run/Work binding is invalid",
                    code="dialogue_question_source_invalid",
                )
        for field, expected in (
            ("agent_dialogue_id", dialogue.id),
            ("agent_dialogue_input_id", input_row.id),
        ):
            actual = metadata.get(field)
            if actual not in (None, "") and _uuid(actual, field) != expected:
                raise AgentDialogueAuthorizationError(
                    "dialogue input question binding is invalid",
                    code="dialogue_question_source_invalid",
                )
        try:
            contract = question_contract_from_message(message, input_kind=input_row.input_kind)
        except AgentDialogueAnswerError as exc:
            raise AgentDialogueValidationError(str(exc), code=exc.code) from exc
        return message, contract

    async def _assert_root_continuation_target(
        self,
        *,
        dialogue: AgentDialogue,
        input_row: Any | None = None,
        root_run: Any | None = None,
        root_work: Any | None = None,
        source_run: Any | None = None,
        source_work: Any | None = None,
    ) -> None:
        """Fence Root mutation tools to the hydrated Human-answer target."""

        from .turn_context import get_turn_context

        turn = get_turn_context()
        context_root_continuation = (
            str(getattr(turn, "agent_dialogue_role", "") or "").casefold()
            == "root"
            and getattr(turn, "agent_dialogue_continuation_target", None)
            is not None
        )
        if root_run is None:
            if context_root_continuation:
                raise AgentDialogueAuthorizationError(
                    "Human continuation target provenance is incomplete",
                    code="dialogue_continuation_target_invalid",
                )
            return
        root_marker = (
            root_run.run_metadata.get("agent_dialogue")
            if isinstance(root_run.run_metadata, Mapping)
            else None
        )
        if (
            not isinstance(root_marker, Mapping)
            or str(root_marker.get("role") or "").casefold() != "root"
            or root_marker.get("source_human_answered") is not True
        ):
            if context_root_continuation:
                raise AgentDialogueAuthorizationError(
                    "Human continuation target provenance is incomplete",
                    code="dialogue_continuation_target_invalid",
                )
            return
        from .turn_context import AgentDialogueContinuationTarget

        target = AgentDialogueContinuationTarget.from_mapping(
            root_marker.get("continuation_target")
        )
        if target is None:
            raise AgentDialogueAuthorizationError(
                "Human continuation target is missing",
                code="dialogue_continuation_target_missing",
            )
        context_target = getattr(turn, "agent_dialogue_continuation_target", None)
        if context_target is not None and context_target.to_dict() != target.to_dict():
            raise AgentDialogueAuthorizationError(
                "Human continuation target context changed",
                code="dialogue_continuation_target_invalid",
            )
        # A persisted Human-answer Root marker is authoritative only when
        # every immutable row in the continuation chain is available.  A
        # missing row must fail closed rather than becoming a skipped
        # ``None`` comparison.
        if any(
            value is None
            for value in (input_row, root_run, root_work, source_run, source_work)
        ):
            raise AgentDialogueAuthorizationError(
                "Human continuation target provenance is incomplete",
                code="dialogue_continuation_target_invalid",
            )
        from .agent_run_service import get_current_agent_run_id

        current_run_id = get_current_agent_run_id()
        if (
            str(target.dialogue_id) != str(dialogue.id)
            or str(target.root_session_id) != str(dialogue.root_session_id)
            or str(target.root_agent_id) != str(dialogue.root_agent_id)
            or str(target.root_agent_revision_id)
            != str(dialogue.root_agent_revision_id)
            or str(target.root_run_id) != str(root_run.id)
            or str(target.root_work_item_id) != str(root_work.id)
            or (
                current_run_id
                and str(target.root_run_id) != str(current_run_id)
            )
            or str(target.input_id) != str(input_row.id)
            or int(target.input_revision) != int(input_row.revision or 0)
            or str(target.control_revision)
            != str(getattr(dialogue, "control_revision", target.control_revision))
            or str(target.source_run_id) != str(source_run.id)
            or str(target.source_session_id) != str(source_run.session_id)
            or str(target.source_agent_id) != str(source_run.agent_id)
            or str(target.source_agent_revision_id)
            != str(source_run.agent_revision_id)
            or str(target.source_session_id) != str(dialogue.worker_session_id)
            or str(target.source_agent_id) != str(dialogue.worker_agent_id)
            or str(target.source_agent_revision_id)
            != str(dialogue.worker_agent_revision_id)
            or str(target.source_work_item_id) != str(source_work.id)
            or str(target.source_work_item_id)
            != str(input_row.source_work_item_id)
            or str(target.source_message_id) != str(input_row.source_message_id)
            or input_row.answer_message_id is None
            or str(target.answer_message_id) != str(input_row.answer_message_id)
        ):
            raise AgentDialogueAuthorizationError(
                "Human continuation target does not match the current Root target",
                code="dialogue_continuation_target_invalid",
            )

    @staticmethod
    def _normalize_dialogue_answer(
        answer: Any,
        contract: AgentDialogueQuestionContract,
    ) -> AgentDialogueAnswer:
        try:
            return parse_agent_dialogue_answer(answer, contract)
        except AgentDialogueAnswerError as exc:
            raise AgentDialogueValidationError(str(exc), code=exc.code) from exc

    async def _session_acl(self, session: Any, session_id: UUID, actor: UUID, *, admin: bool = False) -> bool:
        if admin:
            return True
        row = await session.get(ConversationSession, session_id)
        if row is None or row.deleted_at is not None:
            return False
        if str(row.user_id) == str(actor):
            return True
        participant = (
            await session.execute(
                select(ConversationParticipant).where(
                    ConversationParticipant.session_id == session_id,
                    ConversationParticipant.participant_type == "user",
                    ConversationParticipant.participant_id == str(actor),
                    ConversationParticipant.status == "joined",
                )
            )
        ).scalars().first()
        return participant is not None

    async def _project_acl(self, session: Any, project_id: UUID | None, actor: UUID, permission: str = "read") -> bool:
        if project_id is None:
            return True
        from ..memory.project_repository import ProjectRepository

        return bool(await ProjectRepository.has_permission(session, project_id, actor, permission))

    async def _private_root_for_session(self, session: Any, session_id: UUID) -> AgentPrivateRoot | None:
        """Return the canonical private AoiDots root bound to ``session_id``.

        ``AgentDialogue`` is also used as a generic ledger by a few internal
        callers.  The owner-only rule belongs to the private AoiDots root
        scope, so a missing private-root row deliberately leaves those other
        dialogues on their existing ACL path.
        """

        return (
            await session.execute(
                select(AgentPrivateRoot)
                .where(
                    AgentPrivateRoot.session_id == session_id,
                    AgentPrivateRoot.purpose == "aoidots",
                )
                .limit(1)
            )
        ).scalars().first()

    async def _authorize(self, session: Any, dialogue: AgentDialogue, actor_user_id: Any, actor_role: Any = None, *, permission: str = "read") -> UUID:
        actor = _uuid(actor_user_id, "actor_user_id")
        private_root = await self._private_root_for_session(session, _uuid(dialogue.root_session_id, "root_session_id"))
        if private_root is not None:
            if (
                str(private_root.state or "").casefold() != "active"
                or _uuid(private_root.owner_user_id, "private_root_owner_user_id") != _uuid(dialogue.owner_user_id, "dialogue_owner_user_id")
                or _uuid(private_root.agent_id, "private_root_agent_id") != _uuid(dialogue.root_agent_id, "root_agent_id")
                or _uuid(private_root.session_id, "private_root_session_id") != _uuid(dialogue.root_session_id, "root_session_id")
            ):
                raise AgentDialogueAuthorizationError(
                    "private AoiDots root binding is invalid",
                    code="private_root_binding_invalid",
                )
            root_session = await session.get(ConversationSession, private_root.session_id)
            worker_session = await session.get(ConversationSession, dialogue.worker_session_id)
            if (
                root_session is None
                or root_session.deleted_at is not None
                or str(root_session.user_id) != str(private_root.owner_user_id)
                or root_session.project_id is not None
                or root_session.agent_id not in (None, private_root.agent_id)
                or worker_session is None
                or worker_session.deleted_at is not None
                or str(worker_session.user_id) != str(dialogue.owner_user_id)
                or worker_session.project_id != dialogue.project_id
                or worker_session.agent_id not in (None, dialogue.worker_agent_id)
            ):
                raise AgentDialogueAuthorizationError(
                    "private AoiDots session binding is invalid",
                    code="private_session_binding_invalid",
                )
            if _text(actor_role, limit=32).casefold() == "agent":
                raise AgentDialogueAuthorizationError(
                    "human owner is required for private AoiDots access",
                    code="human_actor_required",
                )
            if actor != _uuid(dialogue.owner_user_id, "dialogue_owner_user_id"):
                raise AgentDialogueAuthorizationError(
                    "private AoiDots dialogue is owner-only",
                    code="private_root_owner_only",
                )
            # An administrator who owns this private root still needs the
            # human owner's canonical Project permission.  The admin role
            # must not turn a private root into a cross-owner read/write
            # channel or bypass ProjectRepository policy.
            if not await self._project_acl(session, dialogue.project_id, actor, permission):
                raise AgentDialogueAuthorizationError("Project access denied", code="project_access_denied")
            return actor
        if _text(actor_role, limit=32).casefold() != "admin":
            if actor != dialogue.owner_user_id and not await self._session_acl(session, dialogue.root_session_id, actor):
                raise AgentDialogueAuthorizationError()
            if not await self._project_acl(session, dialogue.project_id, actor, permission):
                raise AgentDialogueAuthorizationError("Project access denied", code="project_access_denied")
        return actor

    async def _projection(self, session: Any, dialogue: AgentDialogue) -> dict[str, Any]:
        inputs = list(
            (
                await session.execute(
                    select(AgentDialogueInput)
                    .where(AgentDialogueInput.dialogue_id == dialogue.id)
                    .order_by(AgentDialogueInput.revision.desc())
                    .limit(64)
                )
            ).scalars().all()
        )
        deliveries = list(
            (
                await session.execute(
                    select(AgentDialogueDelivery)
                    .where(AgentDialogueDelivery.dialogue_id == dialogue.id)
                    .order_by(AgentDialogueDelivery.sequence.desc())
                    .limit(64)
                )
            ).scalars().all()
        )
        run = await session.get(AgentRun, dialogue.human_run_id) if dialogue.human_run_id else None
        work = await session.get(AgentWorkItem, dialogue.human_work_item_id) if dialogue.human_work_item_id else None
        root_agent = await session.get(Agent, dialogue.root_agent_id)
        worker_agent = await session.get(Agent, dialogue.worker_agent_id)
        root_agent_name = _text(getattr(root_agent, "display_name", None), limit=160) if root_agent is not None else None
        worker_agent_name = _text(getattr(worker_agent, "display_name", None), limit=160) if worker_agent is not None else None

        def _same(left: Any, right: Any) -> bool:
            return left not in (None, "") and right not in (None, "") and str(left) == str(right)

        current_control_revision = int(dialogue.control_revision or 0)

        def _current_delivery(item: Any) -> bool:
            kind = _text(getattr(item, "kind", None), limit=32).casefold()
            expected_control = getattr(item, "expected_control_revision", None)
            if expected_control is None:
                return False
            try:
                expected_control = int(expected_control)
            except (TypeError, ValueError):
                return False
            if expected_control != current_control_revision:
                return False
            if _text(getattr(item, "state", None), limit=32).casefold() in {"cancelled", "expired"}:
                return False
            if kind in {"request", "control"}:
                return (
                    _same(item.source_session_id, dialogue.root_session_id)
                    and _same(item.source_agent_id, dialogue.root_agent_id)
                    and _same(item.source_agent_revision_id, dialogue.root_agent_revision_id)
                    and _same(item.receiver_session_id, dialogue.worker_session_id)
                    and _same(item.receiver_agent_id, dialogue.worker_agent_id)
                    and _same(item.receiver_agent_revision_id, dialogue.worker_agent_revision_id)
                )
            if kind in {"response", "event"}:
                return (
                    _same(item.source_session_id, dialogue.worker_session_id)
                    and _same(item.source_agent_id, dialogue.worker_agent_id)
                    and _same(item.source_agent_revision_id, dialogue.worker_agent_revision_id)
                    and _same(item.receiver_session_id, dialogue.root_session_id)
                    and _same(item.receiver_agent_id, dialogue.root_agent_id)
                    and _same(item.receiver_agent_revision_id, dialogue.root_agent_revision_id)
                )
            return False

        worker_delivery = None
        worker_run = None
        worker_work = None
        for candidate in deliveries:
            if _text(getattr(candidate, "kind", None), limit=32).casefold() not in {"request", "control"} or not _current_delivery(candidate):
                continue
            if candidate.receiver_run_id is None or candidate.receiver_work_item_id is None:
                # A fresh request can be queued before a receiver Run/Work is
                # admitted; the base Dialogue status remains authoritative.
                continue
            candidate_run = await session.get(AgentRun, candidate.receiver_run_id)
            candidate_work = await session.get(AgentWorkItem, candidate.receiver_work_item_id)
            if (
                candidate_run is None
                or candidate_work is None
                or not _same(candidate_run.id, candidate.receiver_run_id)
                or not _same(candidate_run.session_id, dialogue.worker_session_id)
                or not _same(candidate_run.agent_id, dialogue.worker_agent_id)
                or not _same(candidate_run.agent_revision_id, dialogue.worker_agent_revision_id)
                or not _same(candidate_run.work_item_id, candidate_work.id)
                or not _same(candidate_work.conversation_session_id, dialogue.worker_session_id)
                or not _same(candidate_work.assigned_agent_id, dialogue.worker_agent_id)
                or not _same(candidate_work.agent_revision_id, dialogue.worker_agent_revision_id)
                or (
                    candidate_work.active_agent_run_id not in (None, "")
                    and not _same(candidate_work.active_agent_run_id, candidate_run.id)
                )
            ):
                continue
            worker_delivery, worker_run, worker_work = candidate, candidate_run, candidate_work
            break

        # A root response/event from this exact worker generation is the
        # current delivery truth once it exists.  This prevents an older
        # request stuck in ``delivered`` from overriding an acknowledged root
        # receipt after stop/rework or a binding change.
        current_return_delivery = None
        if worker_run is not None and worker_work is not None:
            for candidate in deliveries:
                if _text(getattr(candidate, "kind", None), limit=32).casefold() not in {"response", "event"} or not _current_delivery(candidate):
                    continue
                if (
                    _same(candidate.source_run_id, worker_run.id)
                    and _same(candidate.source_work_item_id, worker_work.id)
                    and _same(candidate.receiver_session_id, dialogue.root_session_id)
                ):
                    current_return_delivery = candidate
                    break
        current_delivery = current_return_delivery or worker_delivery
        budget_owner = None
        if getattr(dialogue, "budget_root_dialogue_id", None) not in (None, ""):
            try:
                budget_owner = await get_dialogue_budget_owner(session, dialogue, lock=False)
            except (DialogueBudgetError, OperationalError):
                # Rolling fixtures may stop before migration 0004.  The
                # current child Run/Work projection remains usable, while a
                # migrated production Dialogue cannot silently authorize a
                # missing aggregate during admission.
                budget_owner = None
        projection = project_dialogue(
            dialogue,
            pending_inputs=inputs,
            artifacts=deliveries,
            run=run,
            work=work,
            worker_run=worker_run,
            worker_work=worker_work,
            worker_delivery=current_delivery,
            budget_owner=budget_owner,
            root_agent_name=root_agent_name,
            worker_agent_name=worker_agent_name,
        )
        input_by_id = {str(item.id): item for item in inputs}
        for item in projection.get("pending_inputs", []):
            row = input_by_id.get(str(item.get("id")))
            if row is None:
                continue
            message_id = row.source_message_id or row.reference_id
            if message_id is None:
                continue
            message_uuid = _uuid(message_id, "question_message_id", required=False)
            if message_uuid is None:
                continue
            message = await session.get(ConversationMessage, message_uuid)
            if message is None or str(message.session_id) != str(dialogue.worker_session_id):
                continue
            try:
                await self._load_input_question_contract(session, dialogue, row)
            except AgentDialogueError:
                continue
            try:
                body = str(message.content or "").strip()
            except Exception:
                body = ""
            metadata = message.message_metadata if isinstance(message.message_metadata, Mapping) else {}
            choices = metadata.get("choices") if isinstance(metadata.get("choices"), list) else []
            item.update(
                {
                    "question": body[:30_000] if body else None,
                    "choices": [str(choice)[:256] for choice in choices[:32]],
                    # ``input_kind`` is the durable routing kind.  It is not
                    # the question UI type and must never leak into the
                    # answer normalizer as ``agent_question``/``human_question``.
                    "input_type": str(metadata.get("input_type") or "free_text")[:64],
                    "allow_multiple": bool(metadata.get("allow_multiple", False)),
                    "allow_free_text": bool(metadata.get("allow_free_text", True)),
                }
            )
        source_delivery = next(
            (delivery for delivery in deliveries if str(getattr(delivery, "kind", "")) == "request" and getattr(delivery, "source_message_id", None)),
            None,
        )
        if source_delivery is not None:
            source_message = await session.get(ConversationMessage, source_delivery.source_message_id)
            if source_message is not None:
                try:
                    source_body = str(source_message.content or "").strip()
                except Exception:
                    source_body = ""
                title = next((line.strip() for line in source_body.splitlines() if line.strip()), "")[:160]
                if title:
                    projection["title"] = title
                    projection["label"] = title
        return projection

    async def bootstrap(self, *, actor_user_id: Any, actor_role: Any = None, context_project_hint_id: Any = None) -> dict[str, Any]:
        if _text(actor_role, limit=32).casefold() == "agent":
            raise AgentDialogueAuthorizationError("human owner is required", code="human_actor_required")
        result = await AgentPrivateProvisioningService(self._manager()).ensure_private_root(
            str(_uuid(actor_user_id, "actor_user_id")),
            config=self.config,
        )
        root = result["root"]
        session_data = result["session"]
        agent = result["agent"]
        db = await self._session()
        try:
            pending_count = int(
                (
                    await db.execute(
                        select(AgentDialogueInput.id)
                        .join(AgentDialogue, AgentDialogue.id == AgentDialogueInput.dialogue_id)
                        .where(
                            AgentDialogue.root_session_id == _uuid(session_data["id"], "root_session_id"),
                            AgentDialogueInput.state == "pending",
                        )
                    )
                ).scalars().all().__len__()
            )
        finally:
            await db.close()
        return project_profile(
            root_id=root["id"],
            session_id=session_data["id"],
            agent_id=agent["id"],
            agent_name=agent.get("display_name") or "AoiDots",
            state=root.get("state") or "active",
            pending_input_count=pending_count,
        )

    async def list_work(self, *, actor_user_id: Any, actor_role: Any = None, root_session_id: Any = None, state: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        actor = _uuid(actor_user_id, "actor_user_id")
        db = await self._session()
        try:
            statement = select(AgentDialogue).where(AgentDialogue.owner_user_id == actor)
            if root_session_id:
                statement = statement.where(AgentDialogue.root_session_id == _uuid(root_session_id, "root_session_id"))
            if state:
                statement = statement.where(AgentDialogue.state == _text(state, limit=32))
            rows = (
                await db.execute(
                    statement.order_by(desc(AgentDialogue.updated_at)).limit(max(1, min(int(limit or 50), 100)))
                )
            ).scalars().all()
            result: list[dict[str, Any]] = []
            for row in rows:
                if row.project_id and not await self._project_acl(db, row.project_id, actor):
                    continue
                result.append(await self._projection(db, row))
            return result
        finally:
            await db.close()

    async def get_dialogue(self, dialogue_id: Any, *, actor_user_id: Any, actor_role: Any = None) -> dict[str, Any]:
        db = await self._session()
        try:
            row = await self._load_dialogue(db, dialogue_id)
            await self._authorize(db, row, actor_user_id, actor_role)
            return await self._projection(db, row)
        finally:
            await db.close()

    @staticmethod
    def _bounded_budgets(value: Mapping[str, Any] | None) -> DialogueBudget:
        """Normalize every supported ceiling and reject unknown aliases."""

        try:
            return normalize_dialogue_budgets(value)
        except DialogueBudgetError as exc:
            raise AgentDialogueValidationError(str(exc), code=exc.code) from exc

    @staticmethod
    def _bounded_deadline(value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            deadline = value
        else:
            try:
                deadline = datetime.fromisoformat(_text(value, limit=96).replace("Z", "+00:00"))
            except ValueError as exc:
                raise AgentDialogueValidationError("deadline is invalid", code="deadline_invalid") from exc
        if deadline.tzinfo is not None:
            deadline = deadline.astimezone(timezone.utc).replace(tzinfo=None)
        now = _now()
        if deadline <= now or deadline > now + timedelta(days=30):
            raise AgentDialogueValidationError("deadline is outside the bounded window", code="deadline_invalid")
        return deadline

    async def _root_anchor_from_context(self, actor_user_id: Any) -> dict[str, Any]:
        from .turn_context import get_turn_context
        from .agent_run_service import get_current_agent_run_id

        context = get_turn_context()
        config = context.agent_execution_config
        source_run_id = get_current_agent_run_id()
        if source_run_id is None and isinstance(config, Mapping):
            source_run_id = config.get("agent_run_id") or config.get("source_run_id") or config.get("human_run_id")
        if not source_run_id:
            raise AgentDialogueAuthorizationError("canonical source AgentRun is required", code="source_run_required")
        db = await self._session()
        try:
            run = await db.get(AgentRun, _uuid(source_run_id, "source_run_id"))
            if run is None or run.trigger_message_id is None:
                raise AgentDialogueAuthorizationError("canonical source Run/message is unavailable", code="source_human_anchor_required")
            trigger = await db.get(ConversationMessage, run.trigger_message_id)
            if trigger is None:
                raise AgentDialogueAuthorizationError("canonical source message is unavailable", code="source_human_anchor_required")
            original_run = run
            original_work_id = run.work_item_id
            message = trigger
            dialogue_id = None
            source_dialogue = None
            source_delivery = None
            run_metadata = run.run_metadata if isinstance(run.run_metadata, Mapping) else {}
            dialogue_meta = run_metadata.get("agent_dialogue") if isinstance(run_metadata.get("agent_dialogue"), Mapping) else {}
            if trigger.role != "user" or trigger.sender_type not in {"user", "human"}:
                dialogue_id = (
                    config.get("agent_dialogue_id")
                    if isinstance(config, Mapping)
                    else None
                ) or dialogue_meta.get("dialogue_id")
                if not dialogue_id:
                    raise AgentDialogueAuthorizationError("receipt-triggered RootRun lacks original Dialogue anchor", code="source_human_anchor_required")
                source_dialogue = await db.get(AgentDialogue, _uuid(dialogue_id, "dialogue_id"))
                if source_dialogue is None or source_dialogue.human_message_id is None or source_dialogue.human_run_id is None:
                    raise AgentDialogueAuthorizationError("Dialogue original human anchor is unavailable", code="source_human_anchor_required")
                # A root Run reached through a worker return must be tied to
                # the latest response/event delivery and its exact receiver
                # Work.  Keep the original Human anchor below for authority;
                # the delivery refs only prove the current root continuation.
                source_delivery = (
                    await db.execute(
                        select(AgentDialogueDelivery)
                        .where(
                            AgentDialogueDelivery.dialogue_id == source_dialogue.id,
                            AgentDialogueDelivery.receiver_run_id == run.id,
                            AgentDialogueDelivery.receiver_session_id == run.session_id,
                            AgentDialogueDelivery.receiver_agent_id == run.agent_id,
                            AgentDialogueDelivery.kind.in_(("response", "event")),
                        )
                        .order_by(AgentDialogueDelivery.sequence.desc())
                        .limit(1)
                    )
                ).scalars().first()
                marker_delivery_id = dialogue_meta.get("delivery_id")
                if (
                    source_delivery is None
                    or (marker_delivery_id not in (None, "") and str(marker_delivery_id) != str(source_delivery.id))
                    or source_delivery.receiver_work_item_id is None
                    or run.work_item_id != source_delivery.receiver_work_item_id
                    or run.agent_revision_id != source_delivery.receiver_agent_revision_id
                ):
                    raise AgentDialogueAuthorizationError(
                        "RootRun latest worker delivery receiver binding is unavailable",
                        code="source_receiver_binding_invalid",
                    )
                receiver_work = await db.get(AgentWorkItem, run.work_item_id)
                if (
                    receiver_work is None
                    or receiver_work.conversation_session_id != run.session_id
                    or receiver_work.assigned_agent_id != run.agent_id
                    or receiver_work.agent_revision_id != run.agent_revision_id
                    or receiver_work.active_agent_run_id not in (None, run.id)
                ):
                    raise AgentDialogueAuthorizationError(
                        "RootRun receiver Work binding is unavailable",
                        code="source_receiver_binding_invalid",
                    )
                message = await db.get(ConversationMessage, source_dialogue.human_message_id)
                original_run = await db.get(AgentRun, source_dialogue.human_run_id)
                original_work_id = source_dialogue.human_work_item_id
                if message is None or original_run is None:
                    raise AgentDialogueAuthorizationError("Dialogue original human anchor is unavailable", code="source_human_anchor_required")
            metadata = message.message_metadata if isinstance(message.message_metadata, Mapping) else {}
            original_metadata = original_run.run_metadata if isinstance(original_run.run_metadata, Mapping) else {}
            binding = original_metadata.get("agent_binding") if isinstance(original_metadata.get("agent_binding"), Mapping) else {}
            authorization = source_dialogue.authorization_json if source_dialogue is not None and isinstance(source_dialogue.authorization_json, Mapping) else {}
            manifest_hash = original_run.execution_manifest_hash or original_metadata.get("execution_manifest_hash") or authorization.get("manifest_hash")
            binding_hash = binding.get("snapshot_hash") or original_metadata.get("binding_hash") or authorization.get("binding_hash")
            raw_capabilities = _SOURCE_CAPABILITY_MISSING
            if "command_capabilities" in metadata:
                raw_capabilities = metadata["command_capabilities"]
            elif "command_capabilities" in original_metadata:
                raw_capabilities = original_metadata["command_capabilities"]
            elif "command_capabilities" in authorization:
                raw_capabilities = authorization["command_capabilities"]
            if raw_capabilities is _SOURCE_CAPABILITY_MISSING:
                capabilities = None
            else:
                capabilities = _source_command_capabilities(raw_capabilities, drop_unsupported=True)
            anchor = {
                "trigger_message_id": str(message.id),
                "sender_type": message.sender_type,
                "sender_id": message.sender_id,
                "message_role": message.role,
                "command_capabilities": capabilities,
                "manifest_hash": manifest_hash,
                "binding_hash": binding_hash,
                "source_run_id": str(run.id),
                "source_work_item_id": str(run.work_item_id) if run.work_item_id else None,
                "human_run_id": str(original_run.id),
                "human_work_item_id": str(original_work_id) if original_work_id else None,
            }
            trigger_proof = {
                "id": str(message.id),
                "role": message.role,
                "sender_type": message.sender_type,
                "sender_id": message.sender_id,
            }
            if capabilities is not None:
                trigger_proof["command_capabilities"] = capabilities
            validated = validate_source_human_anchor(
                anchor,
                actor_user_id=actor_user_id,
                trigger_message=trigger_proof,
            )
            if source_dialogue is not None and source_delivery is not None:
                # These refs are server-derived from the current Root receipt
                # and are added after the public anchor sanitizer.  A caller
                # supplied source_human_anchor cannot smuggle a parent
                # Dialogue or a causal depth into admission.
                validated.update(
                    {
                        "source_dialogue_id": str(source_dialogue.id),
                        "source_dialogue_control_revision": int(source_dialogue.control_revision or 0),
                        "source_delivery_id": str(source_delivery.id),
                        "source_delivery_depth": int(source_delivery.depth or 0),
                    }
                )
            return validated
        finally:
            await db.close()

    async def list_agent_workers(self, *, actor_user_id: Any, actor_role: Any = None, root_session_id: Any = None, project_id: Any = None, limit: int = 32) -> list[dict[str, Any]]:
        private_root = None
        roster_actor_role = actor_role
        db = await self._session()
        try:
            if root_session_id:
                root_session_uuid = _uuid(root_session_id, "root_session_id")
                actor_uuid = _uuid(actor_user_id, "actor_user_id")
                private_root = await self._private_root_for_session(db, root_session_uuid)
                if private_root is not None:
                    if (
                        str(private_root.state or "").casefold() != "active"
                        or _uuid(private_root.owner_user_id, "private_root_owner_user_id") != actor_uuid
                    ):
                        raise AgentDialogueAuthorizationError(
                            "private AoiDots root is owner-only",
                            code="private_root_owner_only",
                        )
                    # ConversationAgentService treats ``admin`` as a
                    # session-management bypass.  Pass the owner path for a
                    # private root so its roster and Project checks use the
                    # same human ACL as every other AoiDots operation.
                    roster_actor_role = None
                elif not await self._session_acl(
                    db,
                    root_session_uuid,
                    actor_uuid,
                    admin=_text(actor_role, limit=32).casefold() == "admin",
                ):
                    raise AgentDialogueAuthorizationError()
        finally:
            await db.close()
        rows = await ConversationAgentService(self._manager(), config=self.config).list_available_agents(
            actor_user_id=actor_user_id,
            actor_role=roster_actor_role,
            session_id=root_session_id,
            project_id=project_id,
            limit=max(1, min(int(limit or 32), 64)),
        )
        if not rows and project_id is None:
            fallback = await AgentPrivateProvisioningService(self._manager()).ensure_private_worker(
                str(_uuid(actor_user_id, "actor_user_id")),
                config=self.config,
            )
            worker = fallback.get("worker_agent") or fallback.get("agent")
            revision = fallback.get("worker_revision") or fallback.get("revision")
            if isinstance(worker, Mapping) and worker.get("id"):
                rows = [
                    {
                        "id": worker["id"],
                        "display_name": worker.get("display_name") or "General Worker",
                        "state": worker.get("state") or "active",
                        "latest_revision_id": revision.get("id") if isinstance(revision, Mapping) else None,
                    }
                ]
        db = await self._session()
        try:
            result: list[dict[str, Any]] = []
            for row in rows:
                revision = None
                revision_id = row.get("latest_revision_id")
                if revision_id:
                    revision = await db.get(AgentRevision, _uuid(revision_id, "revision_id"))
                readiness = self._worker_route_readiness(revision)
                result.append(
                    {
                        "agent_id": row["id"],
                        "agent_name": row["display_name"],
                        "state": row["state"],
                        "revision_id": revision_id,
                        "ready": readiness["ready"],
                        "safe_reason": readiness["safe_reason"],
                    }
                )
            return result
        finally:
            await db.close()

    def _worker_route_readiness(self, revision: Any) -> dict[str, Any]:
        """Read-only preflight for a generic worker before job creation."""

        if revision is None:
            return {"ready": False, "safe_reason": "agent_revision_unavailable"}
        ceiling = getattr(revision, "capability_ceiling_json", None)
        if ceiling is None and isinstance(revision, Mapping):
            ceiling = revision.get("capability_ceiling") or revision.get("capability_ceiling_json")
        capabilities = {str(item).strip() for item in (ceiling or ()) if str(item).strip()}
        if "aoi_tools" not in capabilities:
            return {"ready": False, "safe_reason": "agent_dialogue_capability_unavailable"}
        try:
            from .agent_model_routing import AgentModelRouteError, AgentRoutingPins, resolve_agent_role_route

            pins = AgentRoutingPins.from_revision(revision)
            resolve_agent_role_route(self.config, pins, "general_worker")
        except AgentModelRouteError as exc:
            return {"ready": False, "safe_reason": str(exc.code)}
        except Exception:
            return {"ready": False, "safe_reason": "agent_route_unavailable"}
        return {"ready": True, "safe_reason": None}

    async def _cost_budget_rate_card(
        self,
        revision: Any,
        budgets: DialogueBudget,
    ) -> dict[str, Any] | None:
        """Resolve a server-owned rate card before a priced Dialogue starts.

        Provider-reported, subscription, local, and unknown routes cannot
        prove a configured monetary ceiling before the provider call.  A
        finite cost budget therefore requires an exact flat/tiered server
        pricing rule.  The returned rates use the Dialogue budget helper's
        ``per_1k`` contract and are copied into the immutable Run projection
        for terminal accounting.
        """

        if budgets.max_cost_micros is None:
            return None
        currency = str(budgets.cost_currency or "").strip().upper()
        if currency != "USD":
            raise AgentDialogueValidationError(
                "configured Dialogue cost budgets require the server USD rate card",
                code="dialogue_cost_rate_card_unavailable",
            )
        try:
            from .agent_model_routing import (
                AgentModelRouteError,
                AgentRoutingPins,
                resolve_agent_role_route,
            )
            from .pricing import get_pricing_engine

            route = resolve_agent_role_route(
                self.config,
                AgentRoutingPins.from_revision(revision),
                "general_worker",
            )
            engine = get_pricing_engine()
            await engine.ensure_loaded()
            rule = engine.resolve(route.provider, route.model)
        except AgentModelRouteError as exc:
            raise AgentDialogueValidationError(
                "configured Dialogue cost budget has no resolved provider route",
                code="dialogue_cost_rate_card_unavailable",
            ) from exc
        except Exception as exc:
            raise AgentDialogueValidationError(
                "configured Dialogue cost budget has no server rate card",
                code="dialogue_cost_rate_card_unavailable",
            ) from exc
        if rule is None or str(getattr(rule, "pricing_kind", "")) not in {
            "flat_token",
            "tiered_token",
        }:
            raise AgentDialogueValidationError(
                "configured Dialogue cost budget has no server rate card",
                code="dialogue_cost_rate_card_unavailable",
            )
        rate_sets: list[Mapping[str, Any]] = []
        rates = getattr(rule, "rates", None)
        if isinstance(rates, Mapping):
            rate_sets.append(rates)
        for tier in getattr(rule, "tiers", ()) or ():
            if isinstance(tier, Mapping) and isinstance(tier.get("rates"), Mapping):
                rate_sets.append(tier["rates"])
        if not rate_sets:
            raise AgentDialogueValidationError(
                "configured Dialogue cost budget has no token rates",
                code="dialogue_cost_rate_card_unavailable",
            )

        def _max_per_1k(key: str) -> str | None:
            values: list[Decimal] = []
            for rate_set in rate_sets:
                raw = rate_set.get(key)
                if raw in (None, ""):
                    continue
                try:
                    value = Decimal(str(raw)) / Decimal(1000)
                except (InvalidOperation, TypeError, ValueError, OverflowError):
                    continue
                if value > 0:
                    values.append(value)
            if not values:
                return None
            # Use the highest tier rate for a conservative preflight cap.
            return format(max(values), "f")

        input_rate = _max_per_1k("input")
        output_rate = _max_per_1k("output")
        if input_rate is None or output_rate is None:
            raise AgentDialogueValidationError(
                "configured Dialogue cost budget has incomplete server rates",
                code="dialogue_cost_rate_card_unavailable",
            )
        card: dict[str, Any] = {
            "provider": str(route.provider)[:64],
            "model": str(route.model)[:160],
            "input_per_1k": input_rate,
            "output_per_1k": output_rate,
            "currency": currency,
        }
        cached_rate = _max_per_1k("cached_input")
        if cached_rate is not None:
            card["cached_per_1k"] = cached_rate
        return card

    async def _validate_delegate_source(self, actor_user_id: Any, anchor: Mapping[str, Any]) -> None:
        """Validate active source Run/Work before minting a new request."""

        owner = _uuid(actor_user_id, "actor_user_id")
        run_id = _uuid(anchor.get("source_run_id"), "source_run_id")
        message_id = _uuid(anchor.get("trigger_message_id"), "trigger_message_id")
        db = await self._session()
        try:
            run = await db.get(AgentRun, run_id)
            if run is None or run.status not in _ACTIVE_RUN_STATES or run.parent_run_id is not None:
                raise AgentDialogueAuthorizationError("source AgentRun is not active/current", code="source_run_inactive")
            if str(run.user_id or "") != str(owner):
                raise AgentDialogueAuthorizationError("source AgentRun owner mismatch", code="source_run_owner_mismatch")
            original_run = run
            original_message_id = message_id
            if anchor.get("human_run_id"):
                original_run = await db.get(AgentRun, _uuid(anchor["human_run_id"], "human_run_id"))
                original_message_id = _uuid(anchor.get("trigger_message_id"), "trigger_message_id")
            message = await db.get(ConversationMessage, original_message_id)
            if original_run is None or message is None or message.session_id != original_run.session_id or message.role != "user" or message.sender_type not in {"user", "human"} or str(message.sender_id) != str(owner):
                raise AgentDialogueAuthorizationError("source message is not canonical", code="source_message_invalid")
            source_work_id = anchor.get("source_work_item_id") or run.work_item_id
            if source_work_id:
                work = await db.get(AgentWorkItem, _uuid(source_work_id, "source_work_item_id"))
                if work is None or work.active_agent_run_id not in (None, run.id):
                    raise AgentDialogueAuthorizationError("source Work fence is invalid", code="source_work_invalid")
                if work.active_agent_run_id == run.id and (
                    not work.lease_owner
                    or not work.lease_token
                    or work.lease_expires_at is None
                    or work.lease_expires_at <= _now()
                ):
                    raise AgentDialogueAuthorizationError("source Work lease is not active", code="source_work_lease_invalid")
            if anchor.get("human_work_item_id"):
                original_work = await db.get(AgentWorkItem, _uuid(anchor["human_work_item_id"], "human_work_item_id"))
                if original_work is None or original_work.conversation_session_id != original_run.session_id:
                    raise AgentDialogueAuthorizationError("original human Work anchor is invalid", code="source_work_invalid")
        finally:
            await db.close()

    async def _validate_predecessor_dialogue(
        self,
        db: Any,
        *,
        predecessor_dialogue_id: Any,
        owner: UUID,
        root_session: ConversationSession,
        root_agent: Agent,
        worker: Agent,
        anchor: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate one terminal same-job successor handoff reference."""

        predecessor_id = _uuid(predecessor_dialogue_id, "predecessor_dialogue_id")
        predecessor = (
            await db.execute(
                select(AgentDialogue)
                .where(AgentDialogue.id == predecessor_id)
                .with_for_update()
            )
        ).scalars().first()
        if predecessor is None:
            raise AgentDialogueAuthorizationError(
                "predecessor Dialogue is unavailable",
                code="predecessor_dialogue_not_found",
            )
        if _uuid(predecessor.owner_user_id, "predecessor_owner_user_id") != owner:
            raise AgentDialogueAuthorizationError(
                "predecessor Dialogue owner mismatch",
                code="predecessor_owner_mismatch",
            )
        if predecessor.state not in {"failed", "cancelled", "expired"}:
            raise AgentDialogueConflictError(
                "predecessor Dialogue is not terminal",
                code="predecessor_dialogue_not_terminal",
            )
        if (
            _uuid(predecessor.root_session_id, "predecessor_root_session_id") != _uuid(root_session.id, "root_session_id")
            or _uuid(predecessor.root_agent_id, "predecessor_root_agent_id") != _uuid(root_agent.id, "root_agent_id")
        ):
            raise AgentDialogueAuthorizationError(
                "predecessor Dialogue root binding mismatch",
                code="predecessor_root_binding_invalid",
            )
        if _uuid(predecessor.worker_agent_id, "predecessor_worker_agent_id") != _uuid(worker.id, "worker_agent_id"):
            raise AgentDialogueAuthorizationError(
                "predecessor Dialogue worker Agent mismatch",
                code="predecessor_worker_agent_mismatch",
            )
        old_run = await db.get(AgentRun, predecessor.human_run_id)
        old_message = await db.get(ConversationMessage, predecessor.human_message_id)
        old_work = await db.get(AgentWorkItem, predecessor.human_work_item_id) if predecessor.human_work_item_id else None
        if (
            old_run is None
            or old_message is None
            or _uuid(old_run.user_id, "predecessor_human_run_user_id") != owner
            or _uuid(old_run.session_id, "predecessor_human_run_session_id") != _uuid(root_session.id, "root_session_id")
            or _uuid(old_message.session_id, "predecessor_human_message_session_id") != _uuid(old_run.session_id, "predecessor_human_run_session_id")
            or str(old_message.role or "").casefold() != "user"
            or str(old_message.sender_type or "").casefold() not in {"user", "human"}
            or _uuid(old_message.sender_id, "sender_id") != owner
            or (
                old_work is not None
                and _uuid(old_work.conversation_session_id, "predecessor_human_work_session_id")
                != _uuid(old_run.session_id, "predecessor_human_run_session_id")
            )
        ):
            raise AgentDialogueAuthorizationError(
                "predecessor Dialogue Human authority is invalid",
                code="predecessor_human_authority_invalid",
            )
        run_metadata = old_run.run_metadata if isinstance(old_run.run_metadata, Mapping) else {}
        binding = run_metadata.get("agent_binding") if isinstance(run_metadata.get("agent_binding"), Mapping) else {}
        message_metadata = old_message.message_metadata if isinstance(old_message.message_metadata, Mapping) else {}
        if "command_capabilities" in message_metadata:
            raw_caps = message_metadata["command_capabilities"]
        elif "command_capabilities" in run_metadata:
            raw_caps = run_metadata["command_capabilities"]
        else:
            raw_caps = None
        caps = _source_command_capabilities(raw_caps, drop_unsupported=True) if raw_caps is not None else None
        old_anchor = {
            "trigger_message_id": str(old_message.id),
            "sender_type": old_message.sender_type,
            "sender_id": old_message.sender_id,
            "message_role": old_message.role,
            "command_capabilities": caps,
            "manifest_hash": old_run.execution_manifest_hash or run_metadata.get("execution_manifest_hash"),
            "binding_hash": binding.get("snapshot_hash") or run_metadata.get("binding_hash"),
            "source_run_id": str(old_run.id),
            "source_work_item_id": str(old_work.id) if old_work is not None else None,
        }
        validated_anchor = validate_source_human_anchor(
            old_anchor,
            actor_user_id=owner,
            trigger_message={
                "id": str(old_message.id),
                "role": old_message.role,
                "sender_type": old_message.sender_type,
                "sender_id": old_message.sender_id,
                "command_capabilities": caps,
            },
        )
        authority_refs = {
            "message_id": str(old_message.id),
            "run_id": str(old_run.id),
            "work_item_id": str(old_work.id) if old_work is not None else None,
            "owner_user_id": str(owner),
            "session_id": str(old_run.session_id),
            "binding_hash": validated_anchor["binding_hash"],
            "manifest_hash": validated_anchor["manifest_hash"],
            "command_capabilities": list(validated_anchor["command_capabilities"]),
        }
        authority_digest = hashlib.sha256(
            json.dumps(authority_refs, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        body_digest = hashlib.sha256(str(old_message.content or "").encode("utf-8")).hexdigest()
        return {
            "dialogue_id": str(predecessor.id),
            "state": str(predecessor.state),
            "root_session_id": str(predecessor.root_session_id),
            "root_agent_id": str(predecessor.root_agent_id),
            "root_agent_revision_id": str(predecessor.root_agent_revision_id),
            "worker_agent_id": str(predecessor.worker_agent_id),
            "worker_agent_revision_id": str(predecessor.worker_agent_revision_id),
            "human_message_id": str(predecessor.human_message_id),
            "human_run_id": str(predecessor.human_run_id),
            "human_work_item_id": str(predecessor.human_work_item_id) if predecessor.human_work_item_id else None,
            "control_revision": int(predecessor.control_revision or 0),
            "human_authority": {
                **authority_refs,
                "authority_digest": authority_digest,
                "body_digest": body_digest,
            },
        }

    @staticmethod
    async def _dialogue_budget_schema_available(db: Any) -> bool:
        """Probe the additive chain-budget table without mutating the DB."""

        try:
            connection = await db.connection()
            return bool(
                await connection.run_sync(
                    lambda sync: inspect(sync).has_table(AgentDialogueBudgetRoot.__tablename__)
                )
            )
        except Exception:
            # Focused rolling-upgrade fixtures may intentionally stop at
            # 0003.  Production migration gates reject such a deployment;
            # this compatibility path keeps source-level tests read-only.
            return False

    async def _reserve_root_receipt_chain_budget(
        self,
        db: Any,
        *,
        chain_context: dict[str, Any],
        child_dialogue_id: UUID,
        reservation_key: str,
        receiver_run_id: UUID,
    ) -> None:
        """Reserve one finite child admission in the shared owner row.

        Token/cost capacity is reserved by the canonical receiver Run
        admission.  This early fence consumes only one turn/attempt so a
        second fresh child cannot be created after the owner chain is full.
        Core runtime reuses ``chain_reservation_key`` when it admits the
        child Run and settles that same reservation.
        """

        if not await self._dialogue_budget_schema_available(db):
            raise AgentDialogueAuthorizationError(
                "receipt Dialogue chain budget schema is unavailable",
                code="dialogue_chain_budget_schema_required",
            )
        from .agent_dialogue_budget import DialogueBudgetError, reserve_dialogue_budget

        key = _text(reservation_key, limit=255)
        try:
            await reserve_dialogue_budget(
                db,
                chain_context["budget_owner_dialogue_id"],
                turns=1,
                attempts=1,
                reservation_key=key,
                run_id=receiver_run_id,
            )
        except DialogueBudgetError as exc:
            raise AgentDialogueConflictError(str(exc), code=exc.code) from exc
        chain_context["reservation_key"] = key

    async def _validate_root_receipt_parent(
        self,
        db: Any,
        *,
        anchor: Mapping[str, Any],
        owner: UUID,
        source_run: Any,
        source_work: Any | None,
        root_session: ConversationSession,
        root_agent: Agent,
    ) -> dict[str, Any] | None:
        """Validate server-derived parentage for a receipt-triggered child.

        A normal Human anchor has no ``source_dialogue_id`` and starts an
        independent finite chain.  The only caller that can provide this
        field is ``_root_anchor_from_context`` after it re-read the current
        Root receipt Delivery.  The parent/Delivery/Run/Work rows are still
        re-read here under the admission transaction so a stale callback or a
        forged tool context cannot mint a fresh budget scope.
        """

        parent_id = _uuid(anchor.get("source_dialogue_id"), "source_dialogue_id", required=False)
        if parent_id is None:
            return None
        delivery_id = _uuid(anchor.get("source_delivery_id"), "source_delivery_id", required=False)
        if delivery_id is None:
            raise AgentDialogueAuthorizationError(
                "receipt Dialogue parent Delivery is required",
                code="source_parent_delivery_required",
            )
        parent = (
            await db.execute(
                select(AgentDialogue)
                .where(AgentDialogue.id == parent_id)
                .with_for_update()
            )
        ).scalars().first()
        delivery = await db.get(AgentDialogueDelivery, delivery_id)
        if parent is None or delivery is None or delivery.dialogue_id != parent.id:
            raise AgentDialogueAuthorizationError(
                "receipt Dialogue parent provenance is unavailable",
                code="source_parent_dialogue_invalid",
            )
        if (
            _uuid(parent.owner_user_id, "parent_owner_user_id") != owner
            or _uuid(parent.root_session_id, "parent_root_session_id") != _uuid(root_session.id, "root_session_id")
            or _uuid(parent.root_agent_id, "parent_root_agent_id") != _uuid(root_agent.id, "root_agent_id")
            or _uuid(parent.root_agent_revision_id, "parent_root_agent_revision_id") != _uuid(source_run.agent_revision_id, "source_run_revision_id")
            or str(parent.state or "").casefold() not in {"queued", "running", "waiting"}
            or _uuid(parent.human_run_id, "parent_human_run_id") != _uuid(anchor.get("human_run_id"), "human_run_id")
            or _uuid(parent.human_message_id, "parent_human_message_id") != _uuid(anchor.get("trigger_message_id"), "trigger_message_id")
        ):
            raise AgentDialogueAuthorizationError(
                "receipt Dialogue parent binding is stale",
                code="source_parent_dialogue_invalid",
            )
        if (
            _text(delivery.kind, limit=32).casefold() not in {"response", "event"}
            or _text(delivery.state, limit=32).casefold() in {"failed", "cancelled", "expired"}
            or int(delivery.expected_control_revision or 0) != int(parent.control_revision or 0)
            or _uuid(delivery.receiver_session_id, "delivery_receiver_session_id") != _uuid(root_session.id, "root_session_id")
            or _uuid(delivery.receiver_agent_id, "delivery_receiver_agent_id") != _uuid(root_agent.id, "root_agent_id")
            or _uuid(delivery.receiver_agent_revision_id, "delivery_receiver_revision_id") != _uuid(source_run.agent_revision_id, "source_run_revision_id")
            or _uuid(delivery.receiver_run_id, "delivery_receiver_run_id") != _uuid(source_run.id, "source_run_id")
            or source_work is None
            or _uuid(delivery.receiver_work_item_id, "delivery_receiver_work_item_id") != _uuid(source_work.id, "source_work_item_id")
            or _uuid(source_work.active_agent_run_id, "source_work_run_id", required=False) not in (None, _uuid(source_run.id, "source_run_id"))
        ):
            raise AgentDialogueAuthorizationError(
                "receipt Dialogue parent Delivery binding is stale",
                code="source_parent_delivery_invalid",
            )

        authorization = parent.authorization_json if isinstance(parent.authorization_json, Mapping) else {}
        chain = authorization.get("causal_chain") if isinstance(authorization.get("causal_chain"), Mapping) else {}
        budget_root_id = _uuid(parent.budget_root_dialogue_id, "parent_budget_root_dialogue_id", required=False)
        budget_schema_available = await self._dialogue_budget_schema_available(db)
        if budget_schema_available and budget_root_id is None:
            raise AgentDialogueAuthorizationError(
                "receipt Dialogue budget owner is missing",
                code="source_budget_owner_missing",
            )
        budget_owner = (
            await db.get(AgentDialogueBudgetRoot, budget_root_id)
            if budget_root_id is not None and budget_schema_available
            else None
        )
        if budget_root_id is not None and budget_owner is not None and (
            _uuid(budget_owner.owner_user_id, "budget_owner_user_id") != owner
            or _uuid(budget_owner.root_session_id, "budget_owner_root_session_id") != _uuid(root_session.id, "root_session_id")
            or _uuid(budget_owner.root_agent_id, "budget_owner_root_agent_id") != _uuid(root_agent.id, "root_agent_id")
            or _uuid(budget_owner.root_agent_revision_id, "budget_owner_root_agent_revision_id") != _uuid(source_run.agent_revision_id, "source_run_revision_id")
            or _uuid(budget_owner.human_run_id, "budget_owner_human_run_id") != _uuid(anchor.get("human_run_id"), "human_run_id")
            or _uuid(budget_owner.human_message_id, "budget_owner_human_message_id") != _uuid(anchor.get("trigger_message_id"), "trigger_message_id")
        ):
            raise AgentDialogueAuthorizationError(
                "receipt Dialogue budget owner scope is invalid",
                code="source_budget_owner_invalid",
            )
        chain_id = _text(chain.get("chain_id") or authorization.get("chain_id"), limit=255) or str(budget_root_id or parent.id)
        try:
            chain_depth = int(chain.get("depth") if chain.get("depth") is not None else authorization.get("chain_depth", 0) or 0)
            chain_limit = int(
                chain.get("max_depth")
                if chain.get("max_depth") is not None
                else authorization.get("chain_max_depth", getattr(budget_owner, "max_depth", None))
                or getattr(budget_owner, "max_depth", None)
                or parent.max_depth
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise AgentDialogueAuthorizationError(
                "receipt Dialogue chain budget is invalid",
                code="source_chain_budget_invalid",
            ) from exc
        next_depth = chain_depth + 1
        if next_depth > min(32, chain_limit):
            raise AgentDialogueConflictError(
                "receipt Dialogue chain depth budget is exhausted",
                code="dialogue_chain_depth_exhausted",
            )
        return {
            "chain_id": chain_id,
            "depth": next_depth,
            "max_depth": min(32, chain_limit),
            "budget_owner_dialogue_id": str(budget_root_id or chain.get("budget_owner_dialogue_id") or authorization.get("budget_owner_dialogue_id") or parent.id),
            "parent_deadline": parent.deadline,
            "parent_dialogue_id": str(parent.id),
            "parent_delivery_id": str(delivery.id),
            "parent_control_revision": int(parent.control_revision or 0),
            # Delivery depth is a per-Dialogue hop counter.  A fresh child
            # starts a new delivery lane, so its transport depth follows the
            # server-derived Dialogue causal depth instead.
            "transport_depth": next_depth,
        }

    async def _create_dialogue(
        self,
        *,
        actor_user_id: Any,
        actor_role: Any,
        title: str,
        instruction: str,
        worker_agent_id: Any | None,
        project_id: Any | None,
        task_id: Any | None,
        budgets: DialogueBudget,
        deadline: datetime | None,
        anchor: Mapping[str, Any],
        context_project_hint_id: Any | None,
        predecessor_dialogue_id: Any | None = None,
    ) -> dict[str, Any]:
        owner = _uuid(actor_user_id, "actor_user_id")
        db = await self._session()
        try:
            root = (
                await db.execute(
                    select(AgentPrivateRoot)
                    .where(AgentPrivateRoot.owner_user_id == owner, AgentPrivateRoot.purpose == "aoidots", AgentPrivateRoot.state == "active")
                    .with_for_update()
                )
            ).scalars().first()
            if root is None:
                raise AgentDialogueError("private AoiDots root is unavailable", code="private_root_unavailable")
            root_session = await db.get(ConversationSession, root.session_id)
            root_agent = await db.get(Agent, root.agent_id)
            source_run_id = _uuid(anchor.get("source_run_id"), "source_run_id")
            source_run = await db.get(AgentRun, source_run_id)
            if (
                root_session is None
                or root_agent is None
                or source_run is None
                or root_session.project_id is not None
                or source_run.session_id != root_session.id
                or source_run.agent_id != root_agent.id
                or source_run.parent_run_id is not None
                or source_run.agent_revision_id is None
            ):
                raise AgentDialogueAuthorizationError("private root binding is invalid", code="private_root_invalid")
            # The dialogue must pin the exact immutable revision that admitted
            # this root Run.  Looking up the latest AgentRevision here would
            # make an in-flight Run fail admission after a newer revision is
            # published concurrently, and would make callback/retry refs
            # disagree with the original source Run.
            root_revision = await db.get(AgentRevision, source_run.agent_revision_id)
            if root_revision is None or root_revision.agent_id != root_agent.id:
                raise AgentDialogueAuthorizationError(
                    "source AgentRun revision is not bound to the private root",
                    code="source_revision_invalid",
                )
            source_work = await db.get(AgentWorkItem, source_run.work_item_id) if source_run.work_item_id else None
            chain_context = await self._validate_root_receipt_parent(
                db,
                anchor=anchor,
                owner=owner,
                source_run=source_run,
                source_work=source_work,
                root_session=root_session,
                root_agent=root_agent,
            )
            identity = ConversationAgentService(self._manager(), config=self.config)
            # AoiDots worker admission is scoped to the human owner of the
            # private root.  ConversationAgentService's ``admin`` role is a
            # deliberate cross-session bypass for generic agent management;
            # carrying it into this private scope would bypass AgentProject
            # grants even after the owner's Project ACL was checked above.
            selection_actor_role = None if _text(actor_role, limit=32).casefold() == "admin" else actor_role
            if worker_agent_id:
                worker, worker_revision = await identity.validate_agent_selection(
                    agent_id=worker_agent_id,
                    actor_user_id=actor_user_id,
                    actor_role=selection_actor_role,
                    project_id=project_id,
                    db_session=db,
                )
            else:
                roster = await identity.list_available_agents(
                    actor_user_id=actor_user_id,
                    actor_role=selection_actor_role,
                    session_id=str(root_session.id),
                    project_id=project_id,
                    limit=64,
                    db_session=db,
                )
                selected = next((entry for entry in roster if entry["id"] != str(root_agent.id)), None)
                if selected is None:
                    fallback = await AgentPrivateProvisioningService(self._manager()).ensure_private_worker(
                        str(owner),
                        config=self.config,
                        session_factory=db,
                    )
                    selected = fallback.get("worker_agent") or fallback.get("agent")
                    if not isinstance(selected, Mapping) or not selected.get("id"):
                        raise AgentDialogueAuthorizationError("eligible worker Agent is unavailable", code="worker_agent_unavailable")
                worker, worker_revision = await identity.validate_agent_selection(
                    agent_id=selected["id"], actor_user_id=actor_user_id, actor_role=selection_actor_role, project_id=project_id, db_session=db
                )
            readiness = self._worker_route_readiness(worker_revision)
            if not readiness["ready"]:
                raise AgentDialogueAuthorizationError(
                    "worker execution route is unavailable",
                    code=str(readiness["safe_reason"] or "agent_route_unavailable"),
                )
            server_rate_card = await self._cost_budget_rate_card(
                worker_revision,
                budgets,
            )
            effective_deadline = deadline
            if chain_context is not None and chain_context.get("parent_deadline") is not None:
                parent_deadline = chain_context["parent_deadline"]
                effective_deadline = parent_deadline if effective_deadline is None else min(effective_deadline, parent_deadline)
            predecessor_refs = None
            if predecessor_dialogue_id:
                predecessor_refs = await self._validate_predecessor_dialogue(
                    db,
                    predecessor_dialogue_id=predecessor_dialogue_id,
                    owner=owner,
                    root_session=root_session,
                    root_agent=root_agent,
                    worker=worker,
                    anchor=anchor,
                )
            if worker.id == root_agent.id:
                raise AgentDialogueValidationError("source Agent cannot be its own worker", code="dialogue_self_loop")
            if task_id:
                task_uuid = _uuid(task_id, "task_id")
                project_uuid = _uuid(project_id, "project_id", required=False)
                if project_uuid is None:
                    raise AgentDialogueAuthorizationError("Task requires an explicit Project", code="task_project_required")
                task = await db.get(Task, task_uuid)
                if task is None or task.deleted_at is not None or task.project_id != project_uuid:
                    raise AgentDialogueAuthorizationError("Task is outside the selected Project", code="task_scope_invalid")
                if not await self._project_acl(db, project_uuid, owner, "write"):
                    raise AgentDialogueAuthorizationError("Task Project write permission is required", code="task_project_write_denied")
                assignment = (
                    await db.execute(
                        select(AgentTaskAssignment).where(
                            AgentTaskAssignment.agent_id == worker.id,
                            AgentTaskAssignment.task_id == task_uuid,
                            AgentTaskAssignment.state == "active",
                        ).limit(1)
                    )
                ).scalars().first()
                if assignment is None:
                    raise AgentDialogueAuthorizationError("Worker is not assigned to the Task", code="task_agent_assignment_required")
            dialogue_id = uuid4()
            if chain_context is not None:
                child_request_key = f"dialogue-request:{dialogue_id}:{anchor['source_run_id']}"
                child_receiver_run_id = uuid5(
                    NAMESPACE_URL,
                    f"aoitalk:agent-dialogue-run:{dialogue_id}:{child_request_key}",
                )
                await self._reserve_root_receipt_chain_budget(
                    db,
                    chain_context=chain_context,
                    child_dialogue_id=dialogue_id,
                    reservation_key=f"run:{child_receiver_run_id}",
                    receiver_run_id=child_receiver_run_id,
                )
                chain_context["reservation_key"] = f"run:{child_receiver_run_id}"
                chain_context["receiver_run_id"] = str(child_receiver_run_id)
            child_session_id = uuid4()
            child = ConversationSession(
                id=child_session_id,
                user_id=str(owner),
                character_name=str(worker.display_name)[:200],
                title=_text(title, limit=200),
                project_id=_uuid(project_id, "project_id", required=False),
                agent_id=worker.id,
                parent_session_id=root_session.id,
                context={
                    "kind": "agent_dialogue",
                    "parent_root_session_id": str(root_session.id),
                    "dialogue_id": str(dialogue_id),
                    "source_agent_id": str(root_agent.id),
                    "worker_agent_id": str(worker.id),
                    "context_project_hint_id": _id(context_project_hint_id),
                },
                is_active=True,
                message_count=0,
            )
            db.add(child)
            for participant_type, participant_id, display_name, role, auto_respond in (
                ("user", str(owner), "User", "owner", False),
                ("agent", str(root_agent.id), str(root_agent.display_name)[:200], "member", False),
                ("agent", str(worker.id), str(worker.display_name)[:200], "member", True),
            ):
                db.add(
                    ConversationParticipant(
                        id=uuid4(), session_id=child_session_id, participant_type=participant_type, participant_id=participant_id,
                        display_name=display_name, role=role, status="joined", auto_respond=auto_respond,
                    )
                )
            await db.flush()
            snapshot = await identity._persist_snapshot(db, row=child, agent=worker, revision=worker_revision, actor_id=owner, config=self.config)
            auth = {
                "source_human_message_id": anchor["trigger_message_id"],
                "source_human_sender_id": anchor["sender_id"],
                "command_capabilities": list(anchor["command_capabilities"]),
                "manifest_hash": anchor["manifest_hash"],
                "binding_hash": anchor["binding_hash"],
                "context_project_hint_id": _id(context_project_hint_id),
                "budget": budgets.to_dict(),
            }
            if chain_context is not None:
                auth["causal_chain"] = {
                    "chain_id": chain_context["chain_id"],
                    "depth": chain_context["depth"],
                    "max_depth": chain_context["max_depth"],
                    "budget_owner_dialogue_id": chain_context["budget_owner_dialogue_id"],
                    "parent_dialogue_id": chain_context["parent_dialogue_id"],
                    "parent_delivery_id": chain_context["parent_delivery_id"],
                    "reservation_key": chain_context.get("reservation_key"),
                }
            if server_rate_card is not None:
                auth["budget"]["server_rate_card"] = server_rate_card
            if predecessor_refs is not None:
                auth["predecessor_dialogue"] = predecessor_refs
            auth_hash = hashlib.sha256(json.dumps(auth, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            dialogue = AgentDialogue(
                id=dialogue_id,
                owner_user_id=owner,
                root_session_id=root_session.id,
                root_agent_id=root_agent.id,
                root_agent_revision_id=root_revision.id,
                worker_session_id=child_session_id,
                worker_agent_id=worker.id,
                worker_agent_revision_id=worker_revision.id,
                budget_root_dialogue_id=(
                    _uuid(chain_context["budget_owner_dialogue_id"], "budget_root_dialogue_id")
                    if chain_context is not None
                    else dialogue_id
                ),
                parent_dialogue_id=(
                    _uuid(chain_context["parent_dialogue_id"], "parent_dialogue_id")
                    if chain_context is not None
                    else None
                ),
                causal_depth=int(chain_context["depth"] if chain_context is not None else 0),
                project_id=_uuid(project_id, "project_id", required=False),
                task_id=_uuid(task_id, "task_id", required=False),
                human_message_id=_uuid(anchor["trigger_message_id"], "trigger_message_id"),
                human_run_id=_uuid(anchor.get("human_run_id") or anchor.get("source_run_id"), "human_run_id", required=False),
                human_work_item_id=_uuid(anchor.get("human_work_item_id") or anchor.get("source_work_item_id"), "human_work_item_id", required=False),
                authorization_json=auth,
                authorization_hash=auth_hash,
                max_turns=budgets.max_turns,
                max_depth=budgets.max_depth,
                max_attempts=budgets.max_attempts,
                max_tokens=budgets.max_tokens,
                max_cost_micros=budgets.max_cost_micros,
                cost_currency=budgets.cost_currency,
                deadline=effective_deadline,
                state="queued",
            )
            db.add(dialogue)
            if chain_context is None and await self._dialogue_budget_schema_available(db):
                db.add(
                    AgentDialogueBudgetRoot(
                        root_dialogue_id=dialogue.id,
                        owner_user_id=owner,
                        root_session_id=root_session.id,
                        root_agent_id=root_agent.id,
                        root_agent_revision_id=root_revision.id,
                        human_message_id=dialogue.human_message_id,
                        human_run_id=dialogue.human_run_id,
                        human_work_item_id=dialogue.human_work_item_id,
                        authorization_hash=auth_hash,
                        state="active",
                        max_turns=budgets.max_turns,
                        max_depth=budgets.max_depth,
                        max_attempts=budgets.max_attempts,
                        max_tokens=budgets.max_tokens,
                        max_cost_micros=budgets.max_cost_micros,
                        cost_currency=budgets.cost_currency,
                    )
                )
            source_message_id = uuid5(NAMESPACE_URL, f"aoitalk:dialogue-request:{dialogue.id}:{anchor['source_run_id']}")
            authorization_hash = hashlib.sha256(json.dumps(anchor, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            db.add(
                ConversationMessage(
                    id=source_message_id,
                    session_id=root_session.id,
                    role="assistant",
                    content=_text(instruction, limit=_MAX_INSTRUCTION),
                    client_message_id=f"dialogue-request:{dialogue.id}:{anchor['source_run_id']}",
                    sender_type="agent",
                    sender_id=str(root_agent.id),
                    sender_display_name=str(root_agent.display_name)[:160],
                    message_metadata={
                        "agent_dialogue_id": str(dialogue.id),
                        "source_agent_id": str(root_agent.id),
                        "source_agent_revision_id": str(root_revision.id),
                        "source_human_message_id": anchor["trigger_message_id"],
                        "source_human_authorization_hash": authorization_hash,
                        "control_revision": 0,
                    },
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
            await db.flush()
            # The initial Human Run may have reached a terminal state before
            # this Dialogue row was admitted, so its ordinary terminal
            # callback could not find a Dialogue ledger.  Backfill that exact
            # immutable anchor in this transaction; active Runs are left for
            # their normal terminal callback and return ``False`` here.
            if dialogue.human_run_id is not None:
                await get_agent_dialogue_dispatch_bridge(
                    self._manager(),
                    config=self.config,
                ).record_agent_dialogue_source_usage_in_session(
                    db,
                    dialogue_id=dialogue.id,
                    human_run_id=dialogue.human_run_id,
                )
            digest = hashlib.sha256(_text(instruction, limit=_MAX_INSTRUCTION).encode("utf-8")).hexdigest()
            transport = self._transport()
            delivery = await transport.enqueue_delivery(
                dialogue_id=str(dialogue.id),
                source_session_id=str(root_session.id),
                receiver_session_id=str(child_session_id),
                source_agent_id=str(root_agent.id),
                source_agent_revision_id=str(source_run.agent_revision_id),
                source_run_id=str(anchor["source_run_id"]),
                source_work_item_id=_id(anchor.get("source_work_item_id")),
                source_message_id=str(source_message_id),
                receiver_agent_id=str(worker.id),
                receiver_agent_revision_id=str(worker_revision.id),
                kind="request",
                expected_control_revision=0,
                idempotency_key=f"dialogue-request:{dialogue.id}:{anchor['source_run_id']}",
                request_digest=digest,
                causation_id=(chain_context.get("parent_delivery_id") if chain_context is not None else None),
                depth=(chain_context.get("transport_depth", 0) if chain_context is not None else 0),
                deadline=effective_deadline,
                metadata={
                    "provenance_hash": authorization_hash,
                    "source_human_message_id": anchor["trigger_message_id"],
                    "source_human_authorization_hash": authorization_hash,
                    **(
                        {
                            "causal_chain_id": chain_context["chain_id"],
                            "causal_chain_depth": chain_context["depth"],
                            "parent_dialogue_id": chain_context["parent_dialogue_id"],
                        }
                        if chain_context is not None
                        else {}
                    ),
                },
                session=db,
            )
            await db.commit()
            return {
                "dialogue_id": str(dialogue.id),
                "root_session_id": str(root_session.id),
                "worker_session_id": str(child_session_id),
                "root_agent_id": str(root_agent.id),
                "root_agent_revision_id": str(root_revision.id),
                "worker_agent_id": str(worker.id),
                "worker_agent_revision_id": str(worker_revision.id),
                "binding_id": snapshot.binding_id,
                "status": "queued",
                "chat_link": f"/chat/{child_session_id}",
                "delivery": delivery,
            }
        except Exception:
            with suppress(Exception):
                await db.rollback()
            raise
        finally:
            await db.close()

    async def delegate_agent_work(
        self,
        *,
        actor_user_id: Any,
        actor_role: Any = None,
        title: str,
        instruction: str,
        agent_id: Any = None,
        project_id: Any = None,
        task_id: Any = None,
        context_project_hint_id: Any = None,
        budgets: Mapping[str, Any] | None = None,
        deadline: Any = None,
        source_human_anchor: Mapping[str, Any] | None = None,
        predecessor_dialogue_id: Any | None = None,
    ) -> dict[str, Any]:
        if not _text(title, limit=_MAX_TITLE) or not _text(instruction, limit=_MAX_INSTRUCTION):
            raise AgentDialogueValidationError("title and instruction are required", code="delegate_input_required")
        if context_project_hint_id is None:
            from .turn_context import get_turn_context

            context_project_hint_id = get_turn_context().context_project_hint_id
        anchor = validate_source_human_anchor(source_human_anchor, actor_user_id=actor_user_id) if source_human_anchor else await self._root_anchor_from_context(actor_user_id)
        if not anchor.get("source_run_id"):
            raise AgentDialogueAuthorizationError("canonical source AgentRun is required", code="source_run_required")
        await self._validate_delegate_source(actor_user_id, anchor)
        budget_values = self._bounded_budgets(budgets)
        deadline_value = self._bounded_deadline(deadline)
        db = await self._session()
        try:
            actor_uuid = _uuid(actor_user_id, "actor_user_id")
            checked_projects: set[UUID] = set()
            if context_project_hint_id:
                hint_uuid = _uuid(context_project_hint_id, "context_project_hint_id")
                checked_projects.add(hint_uuid)
                if not await self._project_acl(db, hint_uuid, actor_uuid, "read"):
                    raise AgentDialogueAuthorizationError(
                        "context Project hint is not readable",
                        code="project_hint_access_denied",
                    )
            if project_id:
                project_uuid = _uuid(project_id, "project_id")
                if project_uuid not in checked_projects and not await self._project_acl(db, project_uuid, actor_uuid, "read"):
                    raise AgentDialogueAuthorizationError("Project access denied", code="project_access_denied")
        finally:
            await db.close()
        created = await self._create_dialogue(
            actor_user_id=actor_user_id, actor_role=actor_role, title=title, instruction=instruction, worker_agent_id=agent_id,
            project_id=project_id, task_id=task_id, budgets=budget_values, deadline=deadline_value,
            anchor=anchor, context_project_hint_id=context_project_hint_id,
            predecessor_dialogue_id=predecessor_dialogue_id,
        )
        return {
            "success": True,
            "status": "queued",
            "job_id": created["dialogue_id"],
            "dialogue_id": created["dialogue_id"],
            "chat_link": created["chat_link"],
            "refs": _safe_refs(created),
            "delivery": created.get("delivery"),
        }

    async def get_agent_work_status(self, dialogue_id: Any, *, actor_user_id: Any, actor_role: Any = None) -> dict[str, Any]:
        return await self.get_dialogue(dialogue_id, actor_user_id=actor_user_id, actor_role=actor_role)

    async def enqueue_delivery(
        self,
        dialogue_id: Any,
        *,
        kind: str,
        source_kind: str,
        source_agent_run_id: Any,
        source_message_id: Any = None,
        payload_refs: Mapping[str, Any] | None = None,
        control_revision: int,
        idempotency_key: str,
        request_digest_value: str,
        source_work_item_id: Any = None,
    ) -> dict[str, Any]:
        db = await self._session()
        try:
            from .agent_dialogue_transport import DialogueDeliveryRequest

            dialogue = await self._load_dialogue(db, dialogue_id)
            if source_kind not in {"agent", "root_agent", "worker_agent", "system"}:
                raise AgentDialogueAuthorizationError("delivery source is invalid", code="delivery_source_invalid")
            meta = validate_delivery(dialogue=dialogue, kind=kind, source_agent_run_id=source_agent_run_id, control_revision=control_revision, metadata=payload_refs)
            request = DialogueDeliveryRequest(
                dialogue_id=str(dialogue.id), source_session_id=str(dialogue.root_session_id), receiver_session_id=str(dialogue.worker_session_id),
                source_agent_id=str(dialogue.root_agent_id), source_agent_revision_id=str(dialogue.root_agent_revision_id), source_run_id=str(source_agent_run_id),
                source_work_item_id=_id(source_work_item_id), source_message_id=_id(source_message_id), receiver_agent_id=str(dialogue.worker_agent_id), receiver_agent_revision_id=str(dialogue.worker_agent_revision_id),
                kind=meta["kind"], expected_control_revision=meta["expected_control_revision"], idempotency_key=_text(idempotency_key, limit=255), request_digest=_text(request_digest_value, limit=64), metadata=meta["metadata"],
            )
            return await self._transport().enqueue_delivery(request, session=db)
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code:
                raise AgentDialogueError(str(exc), code=str(code)) from exc
            raise
        finally:
            await db.close()

    async def record_receipt(self, delivery_id: Any, *, receiver_run_id: Any, receiver_work_item_id: Any = None, receiver_message_id: Any = None, response_digest: Any = None, state: str = "succeeded") -> dict[str, Any]:
        try:
            return await self._transport().receipt(str(delivery_id), receiver_run_id=str(receiver_run_id), receiver_work_item_id=_id(receiver_work_item_id), receiver_message_id=_id(receiver_message_id), response_digest=_id(response_digest), state=state)
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code:
                raise AgentDialogueError(str(exc), code=str(code)) from exc
            raise

    async def pause_input(
        self,
        dialogue_id: Any,
        *,
        source_agent_run_id: Any,
        source_work_item_id: Any = None,
        source_message_id: Any = None,
        question: str | None = None,
        question_ref: Any = None,
        input_kind: str = "agent_question",
        input_type: str = "free_text",
        choices: Sequence[str] | None = None,
        allow_multiple: bool = False,
        allow_free_text: bool = True,
        control_revision: int,
    ) -> dict[str, Any]:
        if input_kind not in {"agent_question", "human_question"}:
            raise AgentDialogueValidationError("input kind is not allowlisted", code="input_kind_invalid")
        if question:
            try:
                contract = build_agent_dialogue_question_contract(
                    input_type=input_type,
                    choices=choices,
                    allow_multiple=allow_multiple,
                    allow_free_text=allow_free_text,
                )
            except AgentDialogueAnswerError as exc:
                raise AgentDialogueValidationError(str(exc), code=exc.code) from exc
            result = await get_agent_dialogue_dispatch_bridge(self._manager(), config=self.config).pause_agent_dialogue_for_input(
                run_id=str(source_agent_run_id),
                dialogue_id=str(dialogue_id),
                question=_text(question, limit=_MAX_INSTRUCTION),
                input_type=contract.input_type,
                choices=list(contract.choices),
                allow_multiple=contract.allow_multiple,
                allow_free_text=contract.allow_free_text,
                input_kind=input_kind,
                expected_control_revision=control_revision,
            )
            return dict(result or {})
        raise AgentDialogueValidationError(
            "question body is required for durable input creation",
            code="question_required",
        )

    async def resolve_input(self, dialogue_id: Any, input_id: Any, *, answer: str, client_message_id: Any, expected_control_revision: int, expected_input_revision: int, actor_user_id: Any, actor_role: Any = None, source_kind: str = "human") -> dict[str, Any]:
        if source_kind != "human":
            raise AgentDialogueAuthorizationError("Agent answer requires an explicit transport bridge", code="agent_answer_bridge_required")
        db = await self._session()
        try:
            dialogue = await self._load_dialogue(db, dialogue_id)
            await self._authorize(db, dialogue, actor_user_id, actor_role, permission="write")
            input_row = await db.get(AgentDialogueInput, _uuid(input_id, "input_id"))
            if input_row is None or input_row.dialogue_id != dialogue.id:
                raise AgentDialogueNotFoundError("dialogue input not found", code="input_not_found")
            if int(input_row.revision or 0) != int(expected_input_revision):
                raise AgentDialogueConflictError("dialogue input revision is stale", code="input_revision_stale")
            if int(input_row.expected_control_revision or 0) != int(expected_control_revision):
                raise AgentDialogueConflictError("dialogue input control revision is stale", code="input_control_revision_stale")
            _, question_contract = await self._load_input_question_contract(db, dialogue, input_row)
            normalized_answer = self._normalize_dialogue_answer(answer, question_contract)
        finally:
            await db.close()
        result = await get_agent_dialogue_dispatch_bridge(self._manager(), config=self.config).prepare_agent_dialogue_input_answer(
            input_id=str(input_id), dialogue_id=str(dialogue_id), answer=normalized_answer.canonical, owner_user_id=str(_uuid(actor_user_id, "actor_user_id")), expected_control_revision=expected_control_revision, expected_input_revision=expected_input_revision, client_message_id=_text(client_message_id, limit=512),
        )
        answer_message_id = result.get("answer_message_id")
        if answer_message_id:
            try:
                bridge = get_agent_dialogue_dispatch_bridge(self._manager(), config=self.config)
                dispatch = await bridge.dispatch_dialogue_human_input(
                    dialogue_id=str(dialogue_id),
                    input_id=str(input_id),
                    answer_message_id=str(answer_message_id),
                    owner_user_id=str(_uuid(actor_user_id, "actor_user_id")),
                    expected_control_revision=int(expected_control_revision),
                    expected_input_revision=int(expected_input_revision),
                    client_message_id=_text(client_message_id, limit=512),
                )
                return {**result, "dispatch": dispatch}
            except Exception as exc:
                code = getattr(exc, "code", None)
                if code in _RECOVERABLE_HUMAN_ANSWER_DISPATCH_CODES:
                    # The answer/Input CAS already committed.  A live Root
                    # self-answer provider must finish under its own lease;
                    # surface a durable retryable state instead of turning
                    # the owner's accepted answer into HTTP 409.
                    return {
                        **result,
                        "dispatch": {
                            "success": True,
                            "deferred": True,
                            "reason": str(code),
                        },
                    }
                if code:
                    raise AgentDialogueError(str(exc), code=str(code)) from exc
                raise
        return result

    async def record_human_intervention(self, dialogue_id: Any, *, action: str, instruction: Any = None, actor_user_id: Any, actor_role: Any = None, expected_control_revision: int, source_message_id: Any = None) -> dict[str, Any]:
        db = await self._session()
        try:
            dialogue = await self._load_dialogue(db, dialogue_id)
            await self._authorize(db, dialogue, actor_user_id, actor_role, permission="write")
        finally:
            await db.close()
        if _text(action, limit=32).casefold() == "rework":
            try:
                normalized_instruction = _text(instruction, limit=30_000).strip() or None
                bridge = get_agent_dialogue_dispatch_bridge(self._manager(), config=self.config)
                return dict(await bridge.apply_agent_dialogue_control(
                    dialogue_id=str(dialogue_id),
                    owner_user_id=str(_uuid(actor_user_id, "actor_user_id")),
                    action="rework",
                    expected_control_revision=int(expected_control_revision),
                    actor_kind="human",
                    actor_agent_id=None,
                    actor_agent_revision_id=None,
                    actor_run_id=None,
                    actor_work_item_id=None,
                    human_http=True,
                    instruction=normalized_instruction,
                ) or {})
            except Exception as exc:
                code = getattr(exc, "code", None) or "dialogue_control_action_unavailable"
                raise AgentDialogueError(str(exc), code=str(code)) from exc
        state = "cancelled" if _text(action, limit=32).casefold() == "stop" else "queued"
        try:
            bridge = get_agent_dialogue_dispatch_bridge(self._manager(), config=self.config)
            return dict(await bridge.apply_agent_dialogue_control(
                dialogue_id=str(dialogue_id),
                owner_user_id=str(_uuid(actor_user_id, "actor_user_id")),
                action=_text(action, limit=32).casefold(),
                expected_control_revision=int(expected_control_revision),
                actor_kind="human",
                actor_agent_id=None,
                actor_agent_revision_id=None,
                actor_run_id=None,
                actor_work_item_id=None,
                human_http=True,
            ) or {})
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code:
                raise AgentDialogueError(str(exc), code=str(code)) from exc
            raise

    async def send_agent_work_message(self, dialogue_id: Any, *, message: str, source_agent_run_id: Any) -> dict[str, Any]:
        from .turn_context import get_turn_context

        db = await self._session()
        try:
            dialogue = await self._load_dialogue(db, dialogue_id, lock=True)
            source_run = await db.get(AgentRun, _uuid(source_agent_run_id, "source_run_id"))
            turn = get_turn_context()
            if str(turn.agent_dialogue_role or "").strip().casefold() == "worker":
                return await self._send_worker_work_message(
                    db,
                    dialogue=dialogue,
                    source_run=source_run,
                    message=message,
                    source_agent_run_id=source_agent_run_id,
                )
            if source_run is None or source_run.session_id != dialogue.root_session_id or source_run.agent_id != dialogue.root_agent_id:
                raise AgentDialogueAuthorizationError("source AgentRun is not bound to this dialogue", code="source_run_invalid")
            if source_run.status not in _ACTIVE_RUN_STATES or source_run.parent_run_id is not None:
                raise AgentDialogueAuthorizationError("source AgentRun is not active/current", code="source_run_inactive")
            source_agent_revision_id = source_run.agent_revision_id
            if source_agent_revision_id is None:
                raise AgentDialogueAuthorizationError("source AgentRun revision is missing", code="source_revision_invalid")
            source_work = await db.get(AgentWorkItem, source_run.work_item_id) if source_run.work_item_id else None
            if source_work is None or source_work.active_agent_run_id != source_run.id:
                raise AgentDialogueAuthorizationError("source Work is not current", code="source_work_invalid")
            if not source_work.lease_owner or not source_work.lease_token or source_work.lease_expires_at is None or source_work.lease_expires_at <= _now():
                raise AgentDialogueAuthorizationError("source Work lease is not active", code="source_work_lease_invalid")
            continuation_input = None
            continuation_source_run = None
            continuation_source_work = None
            root_marker = (
                source_run.run_metadata.get("agent_dialogue")
                if isinstance(source_run.run_metadata, Mapping)
                else None
            )
            if (
                isinstance(root_marker, Mapping)
                and root_marker.get("source_human_answered") is True
            ):
                from .turn_context import AgentDialogueContinuationTarget

                target = AgentDialogueContinuationTarget.from_mapping(
                    root_marker.get("continuation_target")
                )
                target_input_id = (
                    _uuid(target.input_id, "input_id", required=False)
                    if target is not None
                    else None
                )
                continuation_input = (
                    await db.get(AgentDialogueInput, target_input_id)
                    if target_input_id is not None
                    else None
                )
                if continuation_input is not None:
                    continuation_source_run = await db.get(
                        AgentRun,
                        continuation_input.source_run_id,
                    )
                    continuation_source_work = (
                        await db.get(
                            AgentWorkItem,
                            continuation_input.source_work_item_id,
                        )
                        if continuation_input.source_work_item_id is not None
                        else None
                    )
            await self._assert_root_continuation_target(
                dialogue=dialogue,
                input_row=continuation_input,
                root_run=source_run,
                root_work=source_work,
                source_run=continuation_source_run,
                source_work=continuation_source_work,
            )
            digest = hashlib.sha256(_text(message, limit=_MAX_INSTRUCTION).encode("utf-8")).hexdigest()
            source_message_id = uuid5(NAMESPACE_URL, f"aoitalk:dialogue-message:{dialogue.id}:{source_run.id}:{digest}")
            if await db.get(ConversationMessage, source_message_id) is None:
                db.add(
                    ConversationMessage(
                        id=source_message_id,
                        session_id=dialogue.root_session_id,
                        role="assistant",
                        content=_text(message, limit=_MAX_INSTRUCTION),
                        client_message_id=f"dialogue-message:{dialogue.id}:{source_run.id}:{digest[:24]}",
                        sender_type="agent",
                        sender_id=str(dialogue.root_agent_id),
                        sender_display_name="AoiDots",
                        message_metadata={"agent_dialogue_id": str(dialogue.id), "source_run_id": str(source_run.id), "control_revision": int(dialogue.control_revision or 0)},
                        created_at=_now(),
                        updated_at=_now(),
                    )
                )
            delivery = await self._transport().enqueue_delivery(
                dialogue_id=str(dialogue.id),
                source_session_id=str(dialogue.root_session_id),
                receiver_session_id=str(dialogue.worker_session_id),
                source_agent_id=str(dialogue.root_agent_id),
                source_agent_revision_id=str(source_agent_revision_id),
                source_run_id=str(source_run.id),
                source_work_item_id=str(source_work.id),
                source_message_id=str(source_message_id),
                receiver_agent_id=str(dialogue.worker_agent_id),
                receiver_agent_revision_id=str(dialogue.worker_agent_revision_id),
                kind="request",
                expected_control_revision=int(dialogue.control_revision or 0),
                idempotency_key=f"dialogue-message:{dialogue.id}:{source_run.id}:{digest[:24]}",
                request_digest=digest,
                metadata={"agent_dialogue_id": str(dialogue.id)},
                session=db,
            )
            await db.commit()
            return {"success": True, "status": "queued", "delivery_id": delivery.get("id"), "transport_work_item_id": delivery.get("transport_work_item_id")}
        except Exception:
            with suppress(Exception):
                await db.rollback()
            raise
        finally:
            await db.close()

    async def _send_worker_work_message(
        self,
        db: Any,
        *,
        dialogue: AgentDialogue,
        source_run: Any,
        message: str,
        source_agent_run_id: Any,
    ) -> dict[str, Any]:
        """Publish one active worker message through the response lane."""

        from .turn_context import get_turn_context

        turn = get_turn_context()
        actor_agent_id = _uuid(turn.agent_id, "agent_id")
        if actor_agent_id != dialogue.worker_agent_id:
            raise AgentDialogueAuthorizationError(
                "worker Agent is not bound to this dialogue",
                code="worker_agent_binding_invalid",
            )
        if _uuid(turn.session_id, "session_id", required=False) != dialogue.worker_session_id:
            raise AgentDialogueAuthorizationError(
                "worker session is not bound to this dialogue",
                code="worker_session_invalid",
            )
        if turn.agent_dialogue_id and _uuid(turn.agent_dialogue_id, "dialogue_id") != dialogue.id:
            raise AgentDialogueAuthorizationError(
                "worker dialogue binding is invalid",
                code="dialogue_binding_invalid",
            )
        if source_run is None or source_run.session_id != dialogue.worker_session_id or source_run.agent_id != dialogue.worker_agent_id:
            raise AgentDialogueAuthorizationError(
                "worker source AgentRun is not bound to this dialogue",
                code="source_run_invalid",
            )
        if source_run.agent_revision_id != dialogue.worker_agent_revision_id:
            raise AgentDialogueAuthorizationError(
                "worker source AgentRevision is not pinned to this dialogue",
                code="source_revision_invalid",
            )
        if source_run.parent_run_id is not None:
            raise AgentDialogueAuthorizationError(
                "worker source AgentRun parent is not allowed",
                code="source_run_parent_forbidden",
            )
        if source_run.status not in _ACTIVE_RUN_STATES:
            raise AgentDialogueAuthorizationError(
                "worker source AgentRun is not active/current",
                code="source_run_inactive",
            )
        source_work = await db.get(AgentWorkItem, source_run.work_item_id) if source_run.work_item_id else None
        if source_work is None or source_work.active_agent_run_id != source_run.id:
            raise AgentDialogueAuthorizationError(
                "worker source Work is not current",
                code="source_work_invalid",
            )
        if source_work.conversation_session_id != dialogue.worker_session_id:
            raise AgentDialogueAuthorizationError(
                "worker source Work session is invalid",
                code="source_work_session_invalid",
            )
        if source_work.assigned_agent_id != dialogue.worker_agent_id:
            raise AgentDialogueAuthorizationError(
                "worker source Work Agent is invalid",
                code="source_work_agent_invalid",
            )
        if source_work.agent_revision_id != dialogue.worker_agent_revision_id:
            raise AgentDialogueAuthorizationError(
                "worker source Work revision is invalid",
                code="source_work_revision_invalid",
            )
        if _text(source_work.state, limit=32).casefold() not in {"claimed", "running", "awaiting_approval", "uncertain"}:
            raise AgentDialogueAuthorizationError(
                "worker source Work is not active",
                code="source_work_inactive",
            )
        if not source_work.lease_owner or not source_work.lease_token or source_work.lease_expires_at is None or source_work.lease_expires_at <= _now():
            raise AgentDialogueAuthorizationError(
                "worker source Work lease is not active",
                code="source_work_lease_invalid",
            )
        body = _text(message, limit=_MAX_INSTRUCTION)
        if not body:
            raise AgentDialogueValidationError("message is required", code="message_required")
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        source_message_id = uuid5(
            NAMESPACE_URL,
            f"aoitalk:dialogue-worker-message:{dialogue.id}:{source_run.id}:{digest}",
        )
        source_message = await db.get(ConversationMessage, source_message_id)
        if source_message is None:
            worker_agent = await db.get(Agent, dialogue.worker_agent_id)
            db.add(
                ConversationMessage(
                    id=source_message_id,
                    session_id=dialogue.worker_session_id,
                    role="assistant",
                    content=body,
                    client_message_id=f"dialogue-worker-message:{dialogue.id}:{source_run.id}:{digest[:24]}",
                    sender_type="agent",
                    sender_id=str(dialogue.worker_agent_id),
                    sender_display_name=str(getattr(worker_agent, "display_name", None) or "Agent")[:160],
                    message_metadata={
                        "agent_dialogue_id": str(dialogue.id),
                        "source_agent_id": str(dialogue.worker_agent_id),
                        "source_agent_revision_id": str(dialogue.worker_agent_revision_id),
                        "agent_run_id": str(source_run.id),
                        "source_run_id": str(source_run.id),
                        "source_work_item_id": str(source_work.id),
                        "delivery_kind": "worker_response",
                        "control_revision": int(dialogue.control_revision or 0),
                    },
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
            await db.flush()
            source_message = await db.get(ConversationMessage, source_message_id) or source_message
        delivery = await self._transport().enqueue_worker_return(
            dialogue_id=str(dialogue.id),
            source_run_id=str(source_run.id),
            source_work_item_id=str(source_work.id),
            source_message_id=str(source_message_id),
            kind="response",
            expected_control_revision=int(dialogue.control_revision or 0),
            request_digest=digest,
            session=db,
        )
        await db.commit()
        return {
            "success": True,
            "status": "queued",
            "delivery_id": delivery.get("id"),
            "transport_work_item_id": delivery.get("transport_work_item_id"),
            "source_message_id": str(source_message_id),
            "source_agent_id": str(dialogue.worker_agent_id),
            "receiver_agent_id": str(dialogue.root_agent_id),
        }

    async def answer_agent_work_question(self, dialogue_id: Any, input_id: Any, *, answer: str, source_agent_run_id: Any, expected_control_revision: int, expected_input_revision: int) -> dict[str, Any]:
        from .turn_context import get_turn_context

        actor_agent = _uuid(get_turn_context().agent_id, "agent_id")
        if not _text(answer, limit=_MAX_INSTRUCTION):
            raise AgentDialogueValidationError("answer is required", code="answer_required")
        db = await self._session()
        try:
            dialogue = await self._load_dialogue(db, dialogue_id, lock=True)
            if actor_agent != dialogue.root_agent_id:
                raise AgentDialogueAuthorizationError("only the root Agent may answer a worker", code="agent_binding_invalid")
            if int(dialogue.control_revision or 0) != int(expected_control_revision):
                raise AgentDialogueConflictError("dialogue control revision is stale", code="dialogue_control_stale")
            input_row = await db.get(AgentDialogueInput, _uuid(input_id, "input_id"))
            if input_row is None or input_row.dialogue_id != dialogue.id:
                raise AgentDialogueNotFoundError("dialogue input not found", code="input_not_found")
            if int(input_row.revision or 0) != int(expected_input_revision):
                raise AgentDialogueConflictError("dialogue input revision is stale", code="input_revision_stale")
            if input_row.input_kind == "human_question" and not (
                input_row.state == "answered" and input_row.answer_message_id is not None
            ):
                raise AgentDialogueAuthorizationError("human decision input must remain with the user", code="human_question_requires_user")

            # The Root Agent's current Run/Work is the authority for the
            # outgoing request.  A Human answer can already have completed
            # the Input, but that user-authored row is never reused as the
            # typed Agent source message below.
            source_run = await db.get(AgentRun, _uuid(source_agent_run_id, "source_run_id"))
            source_work = (
                await db.get(AgentWorkItem, source_run.work_item_id)
                if source_run is not None and source_run.work_item_id is not None
                else None
            )
            if (
                source_run is None
                or source_run.session_id != dialogue.root_session_id
                or source_run.agent_id != dialogue.root_agent_id
                or source_run.agent_revision_id != dialogue.root_agent_revision_id
                or source_run.parent_run_id is not None
                or str(source_run.status or "").casefold()
                not in {"queued", "running", "awaiting_approval"}
                or source_run.user_id not in (None, str(dialogue.owner_user_id))
                or source_work is None
                or source_work.conversation_session_id != dialogue.root_session_id
                or source_work.assigned_agent_id != dialogue.root_agent_id
                or source_work.agent_revision_id != dialogue.root_agent_revision_id
                or source_work.active_agent_run_id != source_run.id
                or str(source_work.state or "").casefold() not in {"claimed", "running"}
                or not source_work.lease_owner
                or not source_work.lease_token
                or source_work.lease_expires_at is None
                or source_work.lease_expires_at <= _now()
            ):
                raise AgentDialogueAuthorizationError(
                    "source AgentRun/Work is not bound to the current root",
                    code="source_run_invalid",
                )
            continuation_source_run = await db.get(
                AgentRun,
                input_row.source_run_id,
            )
            continuation_source_work = (
                await db.get(AgentWorkItem, input_row.source_work_item_id)
                if input_row.source_work_item_id is not None
                else None
            )
            await self._assert_root_continuation_target(
                dialogue=dialogue,
                input_row=input_row,
                root_run=source_run,
                root_work=source_work,
                source_run=continuation_source_run,
                source_work=continuation_source_work,
            )

            _, question_contract = await self._load_input_question_contract(
                db,
                dialogue,
                input_row,
            )
            normalized_answer = self._normalize_dialogue_answer(answer, question_contract)
            root_answer_body = normalized_answer.display_text
            root_answer_digest = canonical_answer_digest(normalized_answer)
            human_answer_message_id = input_row.answer_message_id
            delivery_source_message_id = human_answer_message_id
            if input_row.state == "answered" and input_row.answer_message_id is not None:
                answered_message = await db.get(
                    ConversationMessage,
                    input_row.answer_message_id,
                )
                answered_metadata = (
                    answered_message.message_metadata
                    if answered_message is not None
                    and isinstance(answered_message.message_metadata, Mapping)
                    else {}
                )
                answered_sender_type = str(
                    getattr(answered_message, "sender_type", "") or ""
                ).casefold()
                if (
                    answered_message is not None
                    and answered_sender_type == "agent"
                ):
                    # A previous Root Agent answer is already the canonical
                    # source.  A duplicate call must reuse that row and its
                    # delivery; it must never reinterpret it as a Human row.
                    if input_row.input_kind != "agent_question":
                        raise AgentDialogueAuthorizationError(
                            "human decision input requires the canonical Human answer",
                            code="human_question_requires_user",
                        )
                    if (
                        answered_message.session_id != dialogue.root_session_id
                        or str(answered_message.role or "").casefold() != "assistant"
                        or str(answered_message.sender_id or "")
                        != str(dialogue.root_agent_id)
                        or str(answered_message.content or "") != root_answer_body
                        or str(input_row.answer_digest or "") != root_answer_digest
                        or str(answered_metadata.get("agent_dialogue_id") or "")
                        != str(dialogue.id)
                        or str(answered_metadata.get("agent_dialogue_input_id") or "")
                        != str(input_row.id)
                        or str(answered_metadata.get("agent_run_id") or "")
                        != str(source_run.id)
                        or str(answered_metadata.get("source_work_item_id") or "")
                        != str(source_work.id)
                        or answered_metadata.get("root_agent_answer") is not True
                    ):
                        raise AgentDialogueConflictError(
                            "Root Agent answer binding changed",
                            code="agent_answer_relay_conflict",
                        )
                    existing_delivery = (
                        await db.execute(
                            select(AgentDialogueDelivery).where(
                                AgentDialogueDelivery.dialogue_id == dialogue.id,
                                AgentDialogueDelivery.idempotency_key
                                == f"agent-answer:{input_row.id}:{expected_control_revision}",
                            )
                        )
                    ).scalars().first()
                    if existing_delivery is not None and (
                        existing_delivery.source_run_id != source_run.id
                        or existing_delivery.source_work_item_id != source_work.id
                        or existing_delivery.source_message_id != answered_message.id
                        or existing_delivery.receiver_session_id != dialogue.worker_session_id
                        or existing_delivery.receiver_agent_id != dialogue.worker_agent_id
                        or existing_delivery.receiver_agent_revision_id
                        != dialogue.worker_agent_revision_id
                    ):
                        raise AgentDialogueConflictError(
                            "Root Agent answer delivery binding changed",
                            code="agent_answer_relay_conflict",
                        )
                    delivery_source_message_id = answered_message.id
                else:
                    human_answer_message = answered_message
                    human_answer_metadata = answered_metadata
                    if (
                        human_answer_message is None
                        or human_answer_message.session_id != dialogue.root_session_id
                        or str(human_answer_message.role or "").casefold() != "user"
                        or str(human_answer_message.sender_type or "").casefold()
                        not in {"user", "human"}
                        or str(human_answer_message.sender_id or "")
                        != str(dialogue.owner_user_id)
                        or str(human_answer_metadata.get("agent_dialogue_id") or "")
                        != str(dialogue.id)
                        or str(
                            human_answer_metadata.get("agent_dialogue_input_id") or ""
                        )
                        != str(input_row.id)
                    ):
                        raise AgentDialogueAuthorizationError(
                            "Human answer source is not canonical",
                            code="human_answer_source_invalid",
                        )
                    if input_row.input_kind == "human_question" and (
                        str(human_answer_message.content or "") != root_answer_body
                        or str(input_row.answer_digest or "") != root_answer_digest
                    ):
                        raise AgentDialogueConflictError(
                            "Human decision answer changed",
                            code="human_answer_conflict",
                        )
                    relay_message_id = uuid5(
                        NAMESPACE_URL,
                        f"aoitalk:agent-dialogue-agent-answer-relay:{input_row.id}:{input_row.revision}:{root_answer_digest}",
                    )
                    relay_message = await db.get(ConversationMessage, relay_message_id)
                    if relay_message is None:
                        root_agent = await db.get(Agent, dialogue.root_agent_id)
                        relay_message = ConversationMessage(
                            id=relay_message_id,
                            session_id=dialogue.root_session_id,
                            role="assistant",
                            content=root_answer_body,
                            client_message_id=(
                                f"dialogue-agent-answer-relay:{input_row.id}:"
                                f"{input_row.revision}:{root_answer_digest}"
                            ),
                            sender_type="agent",
                            sender_id=str(dialogue.root_agent_id),
                            sender_display_name=str(
                                getattr(root_agent, "display_name", None) or "Agent"
                            )[:160],
                            message_metadata={
                                "agent_dialogue_id": str(dialogue.id),
                                "agent_dialogue_input_id": str(input_row.id),
                                "input_kind": str(input_row.input_kind),
                                "input_revision": int(input_row.revision or 0),
                                "control_revision": int(dialogue.control_revision or 0),
                                "agent_run_id": str(source_run.id),
                                "source_run_id": str(source_run.id),
                                "source_work_item_id": str(source_work.id),
                                "source_human_answer_message_id": str(
                                    human_answer_message.id
                                ),
                                "source_human_answer_owner_id": str(
                                    dialogue.owner_user_id
                                ),
                                "source_human_answered": True,
                                "root_agent_answer": True,
                            },
                            created_at=_now(),
                            updated_at=_now(),
                        )
                        db.add(relay_message)
                        await db.flush()
                    relay_metadata = (
                        relay_message.message_metadata
                        if isinstance(relay_message.message_metadata, Mapping)
                        else {}
                    )
                    if (
                        relay_message.session_id != dialogue.root_session_id
                        or str(relay_message.role or "").casefold() != "assistant"
                        or str(relay_message.sender_type or "").casefold() != "agent"
                        or str(relay_message.sender_id or "")
                        != str(dialogue.root_agent_id)
                        or str(relay_message.content or "") != root_answer_body
                        or str(relay_metadata.get("agent_dialogue_id") or "")
                        != str(dialogue.id)
                        or str(relay_metadata.get("agent_dialogue_input_id") or "")
                        != str(input_row.id)
                        or str(relay_metadata.get("agent_run_id") or "")
                        != str(source_run.id)
                        or str(relay_metadata.get("source_work_item_id") or "")
                        != str(source_work.id)
                        or str(
                            relay_metadata.get("source_human_answer_message_id") or ""
                        )
                        != str(human_answer_message.id)
                        or str(
                            relay_metadata.get("source_human_answer_owner_id") or ""
                        )
                        != str(dialogue.owner_user_id)
                        or relay_metadata.get("source_human_answered") is not True
                    ):
                        raise AgentDialogueConflictError(
                            "Root Agent answer relay binding changed",
                            code="agent_answer_relay_conflict",
                        )
                    delivery_source_message_id = relay_message.id
            elif input_row.state == "pending":
                answer_digest = root_answer_digest
                answer_message_id = uuid5(NAMESPACE_URL, f"aoitalk:agent-dialogue-agent-answer:{input_row.id}:{answer_digest}")
                if await db.get(ConversationMessage, answer_message_id) is None:
                    db.add(
                        ConversationMessage(
                            id=answer_message_id,
                            session_id=dialogue.root_session_id,
                            role="assistant",
                            content=root_answer_body,
                            client_message_id=f"dialogue-agent-answer:{input_row.id}:{answer_digest}",
                            sender_type="agent",
                            sender_id=str(dialogue.root_agent_id),
                            sender_display_name="AoiDots",
                            message_metadata={
                                "agent_dialogue_id": str(dialogue.id),
                                "agent_dialogue_input_id": str(input_row.id),
                                "input_kind": "agent_question",
                                "input_revision": int(input_row.revision or 0),
                                "control_revision": int(dialogue.control_revision),
                                "agent_run_id": str(source_run.id),
                                "source_run_id": str(source_run.id),
                                "source_work_item_id": str(source_work.id),
                                "root_agent_answer": True,
                            },
                            created_at=_now(),
                            updated_at=_now(),
                        )
                    )
                    await db.flush()
                input_row.state = "answered"
                input_row.answer_message_id = answer_message_id
                input_row.answer_digest = answer_digest
                input_row.updated_at = _now()
                delivery_source_message_id = answer_message_id
            else:
                raise AgentDialogueConflictError("dialogue input is not pending", code="input_not_pending")
            message = await db.get(ConversationMessage, delivery_source_message_id)
            # Transport receipts use the actual decrypted ConversationMessage
            # body.  ``_content`` is encrypted storage and is not the wire
            # digest; Input.answer_digest already carries the canonical wire
            # answer digest separately.
            digest = hashlib.sha256(
                _text(getattr(message, "content", root_answer_body), limit=_MAX_INSTRUCTION).encode("utf-8")
            ).hexdigest()
            delivery = await self._transport().enqueue_delivery(
                dialogue_id=str(dialogue.id),
                source_session_id=str(dialogue.root_session_id),
                receiver_session_id=str(dialogue.worker_session_id),
                source_agent_id=str(dialogue.root_agent_id),
                source_agent_revision_id=str(source_run.agent_revision_id),
                source_run_id=str(source_agent_run_id),
                source_message_id=str(delivery_source_message_id),
                receiver_agent_id=str(dialogue.worker_agent_id),
                receiver_agent_revision_id=str(dialogue.worker_agent_revision_id),
                kind="request",
                expected_control_revision=int(expected_control_revision),
                idempotency_key=f"agent-answer:{input_row.id}:{expected_control_revision}",
                request_digest=digest,
                metadata={"input_id": str(input_row.id), "directive_kind": "finite_agent_answer"},
                session=db,
            )
            dialogue.state = "queued"
            dialogue.updated_at = _now()
            await db.commit()
            return {
                "dialogue_id": str(dialogue.id),
                "input_id": str(input_row.id),
                # For an already Human-answered Input, preserve the canonical
                # user FK while exposing the separate Root Agent relay row.
                "answer_message_id": str(human_answer_message_id or delivery_source_message_id),
                "root_agent_answer_message_id": str(delivery_source_message_id),
                "delivery": delivery,
                "control_revision": int(dialogue.control_revision),
            }
        except AgentDialogueError:
            with suppress(Exception):
                await db.rollback()
            raise
        finally:
            await db.close()

    async def escalate_agent_work_question(self, dialogue_id: Any, input_id: Any, *, expected_control_revision: int, source_agent_run_id: Any | None = None) -> dict[str, Any]:
        """Persist a worker question as a human decision request.

        The root receives a normal encrypted assistant message containing only
        the durable Input/Dialogue references.  No HumanInteraction future is
        created and no dialogue control revision is changed.
        """

        from .turn_context import get_turn_context

        actor_agent_id = _uuid(get_turn_context().agent_id, "agent_id")
        db = await self._session()
        try:
            dialogue = await self._load_dialogue(db, dialogue_id, lock=True)
            if actor_agent_id != dialogue.root_agent_id:
                raise AgentDialogueAuthorizationError("only the root Agent may escalate a question", code="agent_binding_invalid")
            if int(dialogue.control_revision or 0) != int(expected_control_revision):
                raise AgentDialogueConflictError("dialogue control revision is stale", code="dialogue_control_stale")
            input_row = await db.get(AgentDialogueInput, _uuid(input_id, "input_id"))
            if input_row is None or input_row.dialogue_id != dialogue.id:
                raise AgentDialogueNotFoundError("dialogue input not found", code="input_not_found")
            if input_row.state != "pending":
                raise AgentDialogueConflictError("dialogue input is not pending", code="input_not_pending")
            if input_row.expected_control_revision != int(expected_control_revision):
                raise AgentDialogueConflictError("dialogue input is stale", code="input_control_revision_stale")
            from .agent_run_service import get_current_agent_run_id

            current_root_run_id = get_current_agent_run_id()
            current_root_run = (
                await db.get(AgentRun, _uuid(current_root_run_id, "source_run_id"))
                if current_root_run_id
                else None
            )
            current_root_work = (
                await db.get(AgentWorkItem, current_root_run.work_item_id)
                if current_root_run is not None
                and current_root_run.work_item_id is not None
                else None
            )
            continuation_source_run = await db.get(
                AgentRun,
                input_row.source_run_id,
            )
            continuation_source_work = (
                await db.get(AgentWorkItem, input_row.source_work_item_id)
                if input_row.source_work_item_id is not None
                else None
            )
            await self._assert_root_continuation_target(
                dialogue=dialogue,
                input_row=input_row,
                root_run=current_root_run,
                root_work=current_root_work,
                source_run=continuation_source_run,
                source_work=continuation_source_work,
            )
            if input_row.input_kind == "human_question":
                existing = (
                    await db.execute(
                        select(ConversationMessage.id)
                        .where(
                            ConversationMessage.session_id == dialogue.root_session_id,
                            ConversationMessage.client_message_id == f"dialogue-human-question:{input_row.id}",
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                return {"dialogue_id": str(dialogue.id), "input_id": str(input_row.id), "root_message_id": str(existing) if existing else None, "input_kind": "human_question", "control_revision": int(dialogue.control_revision)}
            message_id = uuid5(NAMESPACE_URL, f"aoitalk:human-question:{input_row.id}:{expected_control_revision}")
            message = ConversationMessage(
                id=message_id,
                session_id=dialogue.root_session_id,
                role="assistant",
                content=f"Human decision required for AgentDialogue {dialogue.id}; Input {input_row.id} is pending.",
                client_message_id=f"dialogue-human-question:{input_row.id}",
                sender_type="agent",
                sender_id=str(dialogue.root_agent_id),
                sender_display_name="AoiDots",
                message_metadata={
                    "agent_dialogue_id": str(dialogue.id),
                    "agent_dialogue_input_id": str(input_row.id),
                    "input_kind": "human_question",
                    "control_revision": int(dialogue.control_revision),
                },
                created_at=_now(),
                updated_at=_now(),
            )
            db.add(message)
            input_row.input_kind = "human_question"
            input_row.reference_id = str(message_id)
            input_row.updated_at = _now()
            await db.commit()
            return {
                "dialogue_id": str(dialogue.id),
                "input_id": str(input_row.id),
                "root_message_id": str(message_id),
                "input_kind": "human_question",
                "control_revision": int(dialogue.control_revision),
            }
        except AgentDialogueError:
            with suppress(Exception):
                await db.rollback()
            raise
        finally:
            await db.close()

    async def stop_agent_work(self, dialogue_id: Any, *, expected_control_revision: int) -> dict[str, Any]:
        return await self._agent_control(dialogue_id, "cancelled", expected_control_revision)

    async def resume_agent_work(self, dialogue_id: Any, *, expected_control_revision: int) -> dict[str, Any]:
        return await self._agent_control(dialogue_id, "queued", expected_control_revision)

    async def _agent_control(self, dialogue_id: Any, state: str, expected_control_revision: int) -> dict[str, Any]:
        try:
            from .turn_context import get_turn_context
            from .agent_run_service import get_current_agent_run_id

            context = get_turn_context()
            actor_agent_id = _uuid(context.agent_id, "agent_id")
            actor_user_id = _uuid(context.user_id, "user_id")
            current_run_id = get_current_agent_run_id()
            if actor_agent_id is None or actor_user_id is None or not current_run_id:
                raise AgentDialogueAuthorizationError("current Root Agent turn is required", code="agent_control_actor_required")
            db = await self._session()
            try:
                dialogue = await self._load_dialogue(db, dialogue_id, lock=True)
                if dialogue.root_agent_id != actor_agent_id or dialogue.owner_user_id != actor_user_id:
                    raise AgentDialogueAuthorizationError("only the canonical Root Agent may control this job", code="agent_control_agent_mismatch")
                run = await db.get(AgentRun, _uuid(current_run_id, "source_run_id"))
                if run is None or run.session_id != dialogue.root_session_id or run.agent_id != dialogue.root_agent_id or run.status not in _ACTIVE_RUN_STATES:
                    raise AgentDialogueAuthorizationError("Root Agent Run is not active/current", code="agent_control_run_invalid")
                if run.agent_revision_id is None:
                    raise AgentDialogueAuthorizationError("Root Agent revision is missing", code="agent_control_revision_invalid")
                work = await db.get(AgentWorkItem, run.work_item_id) if run.work_item_id else None
                if work is None or work.active_agent_run_id != run.id or not work.lease_owner or not work.lease_token or work.lease_expires_at is None or work.lease_expires_at <= _now():
                    raise AgentDialogueAuthorizationError("Root Agent Work lease is not active", code="agent_control_work_lease_invalid")
                continuation_input = None
                continuation_source_run = None
                continuation_source_work = None
                run_marker = (
                    run.run_metadata.get("agent_dialogue")
                    if isinstance(run.run_metadata, Mapping)
                    else None
                )
                if (
                    isinstance(run_marker, Mapping)
                    and run_marker.get("source_human_answered") is True
                ):
                    from .turn_context import AgentDialogueContinuationTarget

                    target = AgentDialogueContinuationTarget.from_mapping(
                        run_marker.get("continuation_target")
                    )
                    target_input_id = (
                        _uuid(target.input_id, "input_id", required=False)
                        if target is not None
                        else None
                    )
                    continuation_input = (
                        await db.get(AgentDialogueInput, target_input_id)
                        if target_input_id is not None
                        else None
                    )
                    if continuation_input is not None:
                        continuation_source_run = await db.get(
                            AgentRun,
                            continuation_input.source_run_id,
                        )
                        continuation_source_work = (
                            await db.get(
                                AgentWorkItem,
                                continuation_input.source_work_item_id,
                            )
                            if continuation_input.source_work_item_id is not None
                            else None
                        )
                await self._assert_root_continuation_target(
                    dialogue=dialogue,
                    input_row=continuation_input,
                    root_run=run,
                    root_work=work,
                    source_run=continuation_source_run,
                    source_work=continuation_source_work,
                )
            finally:
                await db.close()
            action = "stop" if state == "cancelled" else "resume"
            return dict(await get_agent_dialogue_dispatch_bridge(self._manager(), config=self.config).apply_agent_dialogue_control(
                dialogue_id=str(dialogue_id),
                owner_user_id=str(actor_user_id),
                action=action,
                expected_control_revision=int(expected_control_revision),
                actor_kind="agent",
                actor_agent_id=str(actor_agent_id),
                actor_agent_revision_id=str(run.agent_revision_id),
                actor_run_id=str(run.id),
                actor_work_item_id=str(work.id),
            ) or {})
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code:
                raise AgentDialogueError(str(exc), code=str(code)) from exc
            raise

    async def get_turn_context(self, dialogue_id: Any, *, actor_user_id: Any, actor_role: Any = None) -> dict[str, Any]:
        db = await self._session()
        try:
            dialogue = await self._load_dialogue(db, dialogue_id)
            await self._authorize(db, dialogue, actor_user_id, actor_role)
            inputs = list((await db.execute(select(AgentDialogueInput).where(AgentDialogueInput.dialogue_id == dialogue.id, AgentDialogueInput.state == "pending").order_by(AgentDialogueInput.revision.desc()).limit(64))).scalars().all())
            return build_turn_context(dialogue, pending_inputs=inputs)
        finally:
            await db.close()


__all__ = [
    "AgentDialogueAuthorizationError",
    "AgentDialogueConflictError",
    "AgentDialogueError",
    "AgentDialogueNotFoundError",
    "AgentDialogueService",
    "AgentDialogueValidationError",
    "get_agent_dialogue_dispatch_bridge",
    "validate_delivery",
    "validate_source_human_anchor",
]
