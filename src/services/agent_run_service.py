"""Service layer for durable agent run tracking."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Mapping
from contextvars import ContextVar, Token
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict

from sqlalchemy import and_, case, delete, desc, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..memory.database import get_database_manager
from ..memory.project_repository import ProjectRepository
from ..memory.models import (
    Agent,
    AgentProjectGrant,
    AgentRevision,
    AgentRun,
    AgentRunEdge,
    AgentRunEvent,
    AgentRunToolCall,
    AgentDialogue,
    AgentDialogueBudgetRoot,
    AgentDialogueDelivery,
    AgentDialogueInput,
    AgentDialogueReservation,
    AgentDialogueUsage,
    AGENT_DIALOGUE_INPUT_KINDS,
    AgentWorkEvent,
    AgentWorkItem,
    ConversationDispatchOutbox,
    ConversationMessage,
    ConversationSession,
    Task,
    User,
)
from ..memory.models.operations import _normalized_security_key, sanitize_source_url
from .agent_team_v3 import (
    AGENT_TEAM_CAPABILITY_CATALOG,
    AGENT_TEAM_DEFAULT_TEAMS,
    AGENT_TEAM_SUBAGENT_CATALOG,
)
from .agent_resource_mutations import (
    DOCS_MUTATION_OPERATIONS,
    TASK_MUTATION_OPERATIONS,
    build_agent_resource_mutations,
)
from .agent_run_operation_identity import canonical_event_operation_key
from .agent_dialogue_answer import (
    AgentDialogueAnswerError,
    build_agent_dialogue_question_contract,
    canonical_answer_digest,
    parse_agent_dialogue_answer,
    question_contract_from_message,
)
from ..utils.uuid_utils import parse_uuid

logger = logging.getLogger(__name__)

_EXPECTED_AGENT_OMITTED = object()

RUN_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
RUN_ACTIVE_STATUSES = {"queued", "running", "awaiting_approval", "uncertain"}
_current_agent_run_id: ContextVar[str | None] = ContextVar(
    "aoitalk_current_agent_run_id",
    default=None,
)
_DISPATCH_PROCESS_ID = str(uuid.uuid4())
MAX_CLIENT_MESSAGE_ID_LENGTH = 512
DISPATCH_OUTBOX_RETENTION_SECONDS = 7 * 24 * 60 * 60
# Dispatch attempts are intentionally bounded.  Existing callers may still
# release a lease for an immediate retry; once this ceiling is reached the
# row is dead-lettered and the AgentRun is terminalized instead of looping
# forever after a restart.
DISPATCH_MAX_ATTEMPTS = 5
DISPATCH_DEADLETTER_STATUS = "deadletter"
_ROOT_DIALOGUE_BUDGET_BLOCK_CODES = frozenset(
    {
        "dialogue_turn_budget_exhausted",
        "dialogue_attempt_budget_exhausted",
        "dialogue_token_budget_exhausted",
        "dialogue_cost_budget_exhausted",
        "dialogue_token_usage_unknown",
        "dialogue_cost_usage_unknown",
        "dialogue_cost_rate_card_unavailable",
    }
)
_AGENT_RUN_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "total_tokens",
)
_RESOURCE_MUTATION_TOOL_NAMES = frozenset(
    (*TASK_MUTATION_OPERATIONS, *DOCS_MUTATION_OPERATIONS)
)
_PREPROVIDER_SAFE_ERROR_CODE = "agent_dialogue_provider_not_started"


@dataclass(frozen=True)
class PreproviderFailureProof:
    """Server-created proof that one local turn never entered its SDK sender."""

    run_id: str
    proof_id: str
    request_type: str
    provider_started: bool = False
    sender_attempts: int = 0
    safe_error_code: str = _PREPROVIDER_SAFE_ERROR_CODE

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "proof_id": self.proof_id,
            "provider_started": self.provider_started,
            "sender_attempts": self.sender_attempts,
            "request_type": self.request_type,
            "safe_error_code": self.safe_error_code,
        }


def _trusted_preprovider_failure_proof(
    typed_proof: Any,
    run_id: Any,
) -> dict[str, Any] | None:
    """Validate only the non-JSON server proof for a zero-sender turn."""

    expected_run_id = str(run_id or "")
    if not expected_run_id or not isinstance(typed_proof, PreproviderFailureProof):
        return None
    if (
        typed_proof.run_id != expected_run_id
        or typed_proof.provider_started is not False
        or typed_proof.sender_attempts != 0
        or typed_proof.safe_error_code != _PREPROVIDER_SAFE_ERROR_CODE
        or not re.fullmatch(r"[0-9a-f]{32}", typed_proof.proof_id)
        or typed_proof.request_type not in {"chat", "stream", "retry", "tool"}
    ):
        return None
    return typed_proof.to_dict()


def _is_postgres_lock_not_available(error: BaseException) -> bool:
    """Recognize PostgreSQL NOWAIT contention without masking DB failures."""

    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for candidate in (current, getattr(current, "orig", None)):
            if candidate is None:
                continue
            for attribute in ("sqlstate", "pgcode", "sql_state"):
                if str(getattr(candidate, attribute, "") or "") == "55P03":
                    return True
        cause = current.__cause__ or current.__context__
        current = cause if isinstance(cause, BaseException) else None
    return False


@dataclass
class PreparedApprovedMutationReceipt:
    """Transaction-local fence for one approved resource mutation.

    ``owned`` is true only for the caller that successfully inserted the
    unique ``(run_id, tool_call_id)`` row and may therefore perform the
    resource write.  A false value represents an already committed winner;
    callers must return ``receipt``'s prior result without touching the
    resource.
    """

    run_id: uuid.UUID
    tool_name: str
    tool_call_id: str
    receipt: AgentRunToolCall
    owned: bool
    metadata: dict[str, Any]


def _monotonic_activity(value):
    """Advance a session activity marker without regressing concurrent work."""
    return case(
        (ConversationSession.last_activity.is_(None), value),
        (ConversationSession.last_activity < value, value),
        else_=ConversationSession.last_activity,
    )


class DispatchConflictError(ValueError):
    """An idempotency key was reused by another principal or request."""

    code = "dispatch_conflict"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = str(code or self.code)
        super().__init__(message)


class AgentChatAdmissionError(ValueError):
    """Typed, API-safe Chat Agent admission denial."""

    def __init__(self, message: str, *, code: str) -> None:
        self.code = str(code)
        super().__init__(message)


def _check_expected_agent_binding(
    conversation: Any,
    *,
    expected_agent_id: Any = _EXPECTED_AGENT_OMITTED,
    expected_agent_provided: bool | None = None,
) -> None:
    """Fence an optional client observation against the locked session row."""

    provided = (
        expected_agent_id is not _EXPECTED_AGENT_OMITTED
        if expected_agent_provided is None
        else bool(expected_agent_provided)
    )
    if not provided:
        return
    expected = None
    if expected_agent_id not in (None, ""):
        expected = parse_uuid(expected_agent_id)
        if expected is None:
            raise AgentChatAdmissionError(
                "invalid expected_agent_id",
                code="agent_admission_invalid",
            )
    canonical = parse_uuid(getattr(conversation, "agent_id", None))
    if canonical != expected:
        raise DispatchConflictError(
            "conversation Agent binding changed",
            code="agent_binding_changed",
        )


def _mutation_confirmation_for_tool(
    tool_name: str,
    requested: bool,
    success: bool,
) -> bool:
    """Classify durable mutation tools even when stream results omit the flag.

    Some live tool streams do not include ``mutation_confirmed`` in their
    result payload. The tool name is the authoritative audit classification;
    success is persisted separately and still gates card extraction.
    """

    normalized_name = str(tool_name or "").rsplit(".", 1)[-1].strip().lower()
    return bool(requested) or (
        bool(success) and normalized_name in _RESOURCE_MUTATION_TOOL_NAMES
    )


def _normalized_agent_run_usage(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    usage: dict[str, int] = {}
    for key in _AGENT_RUN_USAGE_FIELDS:
        raw = value.get(key)
        if raw is None:
            continue
        try:
            usage[key] = max(0, int(raw))
        except (TypeError, ValueError):
            continue
    if not usage:
        return None
    if isinstance(value.get("output_includes_reasoning"), bool):
        usage["output_includes_reasoning"] = value[
            "output_includes_reasoning"
        ]
    if value.get("total_tokens_source") in {"reported", "derived"}:
        usage["total_tokens_source"] = value["total_tokens_source"]
    reasoning_is_separate = not bool(
        usage.get("output_includes_reasoning", False)
    )
    usage.setdefault(
        "total_tokens",
        usage.get("input_tokens", 0)
        + usage.get("output_tokens", 0)
        + (
            usage.get("reasoning_tokens", 0)
            if reasoning_is_separate
            else 0
        ),
    )
    return usage


def _merge_agent_run_usage(
    current: dict[str, Any] | None,
    incoming: dict[str, Any],
) -> dict[str, Any]:
    left = _normalized_agent_run_usage(current) or {}
    right = _normalized_agent_run_usage(incoming) or {}
    merged: dict[str, Any] = {
        key: int(left.get(key, 0)) + int(right.get(key, 0))
        for key in _AGENT_RUN_USAGE_FIELDS
    }
    left_flag = left.get("output_includes_reasoning")
    right_flag = right.get("output_includes_reasoning")
    if isinstance(left_flag, bool) and isinstance(right_flag, bool):
        if left_flag == right_flag:
            merged["output_includes_reasoning"] = left_flag
    elif isinstance(right_flag, bool):
        merged["output_includes_reasoning"] = right_flag
    elif isinstance(left_flag, bool):
        merged["output_includes_reasoning"] = left_flag
    left_source = left.get("total_tokens_source")
    right_source = right.get("total_tokens_source")
    if left_source == right_source and left_source in {"reported", "derived"}:
        merged["total_tokens_source"] = left_source
    elif right_source in {"reported", "derived"}:
        merged["total_tokens_source"] = right_source
    return merged


def dispatch_client_message_key(client_message_id: str) -> str:
    """Normalize a bounded client id into a fixed-width DB key."""
    normalized = str(client_message_id or "").strip()
    if not normalized:
        raise ValueError("client_message_id is required")
    if len(normalized) > MAX_CLIENT_MESSAGE_ID_LENGTH:
        raise ValueError("client_message_id is too long")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

AGENT_TEAM_TOOL_SUBAGENTS = {
    "agent_team_delegate": "agent_team",
}

# Read-only history compatibility.  These names are never registered by the
# runtime; they only let old AgentRun rows retain a useful actor label.
_LEGACY_HISTORY_TOOL_ACTORS = {
    "writing_assistant": ("agent_team", "story_writer", "執筆"),
    "import_assistant": ("agent_team", "story_import", "Story取り込み"),
    "utility_assistant": ("integration", "utility", "補助機能"),
    "spotify_assistant": ("integration", "spotify", "Spotify連携"),
}

_SPOTIFY_TOOL_NAMES = frozenset(
    {
        "setup_spotify_auth", "set_spotify_auth_code", "search_spotify_activity",
        "get_spotify_activity_stats", "get_recent_spotify_activity",
        "get_spotify_listening_patterns", "search_spotify_music",
        "play_spotify_track", "play_song_now", "queue_song", "pause_spotify",
        "skip_spotify_track", "previous_track", "get_spotify_status", "show_queue",
        "clear_spotify_queue", "remove_from_queue", "get_spotify_user_playlists",
        "create_playlist", "create_playlist_from_queue", "add_tracks_to_playlist",
        "add_queue_to_playlist", "add_playlist_to_queue", "remove_tracks_from_playlist",
        "play_playlist",
    }
)

DIRECT_TOOL_LABELS = {
    "web_search": "Web検索",
    "search_web": "Web検索",
    "shell_command": "シェルコマンド",
    "deep_research": "Deep Research",
    "get_weather": "天気",
    "get_weather_info": "天気",
    "get_current_time": "現在時刻",
    "calculate": "計算",
    "create_task": "タスク作成",
    "update_task": "タスク更新",
    "list_tasks": "タスク取得",
    "list_project_information": "案件情報参照",
    "get_project_context": "案件コンテキスト参照",
    "list_record_tables": "台帳参照",
    "read_file": "ファイル読み取り",
    "write_file": "ファイル書き込み",
    "execute_code": "コード実行",
    "generate_image": "画像生成",
    "media_assistant": "メディア連携",
}

SENSITIVE_TOOL_RESULT_NAMES = {
    "webex_get_thread",
    "webex_search_messages",
}
SENSITIVE_TOOL_RESULT_MARKER = (
    "[Webexメッセージ本文は一時利用のため実行履歴へ保存しません]"
)
AUDIT_REDACTED_MARKER = "[REDACTED]"
AUDIT_REDACTION_FAILED_MARKER = "[REDACTED_UNAVAILABLE]"
# Cloud Advisor is a read-only advisory capability.  Its response is useful
# to the live parent, but the durable AgentRun/audit surfaces must retain only
# routing and outcome metadata.  Keep the run-type check narrow so ordinary
# chat/director history remains backwards compatible.
CLOUD_ADVISOR_RUN_TYPES = frozenset(
    {
        "cloud_advisor",
        "cloud_advisor_consultation",
        "cloud_advisor_consult",
    }
)
_STATUS_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}
_STATUS_LOCKS_GUARD = threading.Lock()


def _status_lock(run_id: uuid.UUID) -> asyncio.Lock:
    """Serialize same-run status transitions in one event loop."""

    loop_id = id(asyncio.get_running_loop())
    key = (loop_id, str(run_id))
    with _STATUS_LOCKS_GUARD:
        lock = _STATUS_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _STATUS_LOCKS[key] = lock
        return lock


def _release_status_lock(run_id: uuid.UUID, lock: asyncio.Lock) -> None:
    """Release and prune an idle per-run lock cache entry."""

    lock.release()
    key = (id(asyncio.get_running_loop()), str(run_id))
    waiters = getattr(lock, "_waiters", None)
    with _STATUS_LOCKS_GUARD:
        if (
            _STATUS_LOCKS.get(key) is lock
            and not lock.locked()
            and not waiters
        ):
            _STATUS_LOCKS.pop(key, None)


def _validate_typed_agent_actor(
    *,
    user_id: str | None,
    agent_id: uuid.UUID | None,
    agent_revision: Any | None,
    acting_subagent_id: str | None,
    agent_row: Any | None = None,
    config: Any | None = None,
) -> None:
    """Enforce the no-Agent-as-user and pinned subagent boundary.

    This helper is called for both ordinary AgentRun creation and the
    conversation dispatch transaction.  A UUID that belongs to an Agent may
    never be copied into the legacy ``user_id`` column, even when the caller
    omitted ``agent_id``.  Subagent IDs are executable declarations only when
    they are present in the exact pinned revision and that revision's Team.
    """

    if user_id and agent_row is not None:
        candidate = parse_uuid(user_id)
        if candidate is not None and getattr(agent_row, "id", None) == candidate:
            raise ValueError("Agent IDs must not be stored in user_id")
    if not acting_subagent_id:
        return
    normalized = str(acting_subagent_id).strip()
    known_subagent_ids = set(AGENT_TEAM_SUBAGENT_CATALOG)
    if config is not None:
        try:
            from .agent_team_v3 import agent_team_v3_subagents

            known_subagent_ids.update(
                str(item.get("subagent_id"))
                for item in agent_team_v3_subagents(config)
                if isinstance(item, Mapping) and item.get("subagent_id")
            )
        except Exception:
            pass
    if normalized not in known_subagent_ids:
        raise ValueError("unknown acting_subagent_id")
    # Historical human/chat runs may carry a specialist marker without a
    # typed AgentRevision.  Preserve that compatibility path; the strict
    # revision/team binding applies whenever an autonomous Agent identity is
    # present.
    if agent_id is None:
        return
    if agent_revision is None:
        raise ValueError("acting_subagent_id requires an exact AgentRevision")
    allowed = {
        str(item).strip()
        for item in (getattr(agent_revision, "allowed_subagent_ids", None) or [])
        if str(item).strip()
    }
    team_id = str(getattr(agent_revision, "agent_team_id", "") or "")
    team = AGENT_TEAM_DEFAULT_TEAMS.get(team_id) or {}
    if config is not None:
        try:
            from .agent_team_v3 import agent_team_v3_teams

            configured_teams = {
                str(item.get("team_id")): item
                for item in agent_team_v3_teams(config)
                if isinstance(item, Mapping) and item.get("team_id")
            }
            team = configured_teams.get(team_id) or team
        except Exception:
            # An unavailable/malformed optional config must not broaden the
            # default Team membership.
            pass
    team_members = {
        str(item).strip() for item in (team.get("subagent_ids") or []) if str(item).strip()
    }
    if normalized not in allowed or normalized not in team_members:
        raise ValueError("acting_subagent_id is not allowed by the pinned AgentRevision")


def _validate_manifest_bindings(
    manifest: Dict[str, Any] | None,
    *,
    agent_id: uuid.UUID | None,
    revision_id: uuid.UUID | None,
    task_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
    work_item_id: uuid.UUID | None = None,
) -> None:
    """Ensure a metadata manifest cannot claim a different typed target."""

    if not isinstance(manifest, dict):
        return
    for key, expected in (
        ("agent_id", agent_id),
        ("agent_revision_id", revision_id),
        ("task_id", task_id),
        ("work_item_id", work_item_id),
        ("project_id", project_id),
    ):
        raw = manifest.get(key)
        if raw in (None, ""):
            continue
        parsed = parse_uuid(raw)
        if parsed is None or expected is None or parsed != expected:
            raise ValueError(f"execution manifest {key} does not match typed run binding")


async def _lookup_agent_for_legacy_user(session: Any, user_uuid: uuid.UUID) -> Any | None:
    """Look up an Agent without breaking pre-WS01 rolling schemas.

    Before migration ``agents`` does not exist in a few lightweight legacy
    fixtures.  A human-only run remains valid in that window; any other DB
    error is propagated so production does not silently bypass an unavailable
    identity authority.
    """

    try:
        return await session.get(Agent, user_uuid)
    except OperationalError as exc:
        detail = str(exc).casefold()
        if "no such table" in detail and "agent" in detail:
            return None
        if "undefinedtable" in detail and "agent" in detail:
            return None
        raise


async def _pin_session_agent_binding(
    session: Any,
    conversation: Any,
    *,
    user_id: str | None,
    requested_agent_id: str | None = None,
    requested_revision_id: str | None = None,
    config: Any | None = None,
) -> tuple[uuid.UUID | None, uuid.UUID | None, Any | None]:
    """Create one exact immutable turn snapshot in the caller transaction."""

    canonical_agent_id = getattr(conversation, "agent_id", None)
    if canonical_agent_id is None:
        if requested_agent_id:
            raise DispatchConflictError(
                "session Agent binding is required for chat execution",
                code="agent_binding_changed",
            )
        return None, None, None
    from .conversation_agent_service import pin_conversation_agent_turn

    actor_uuid = parse_uuid(user_id)
    if actor_uuid is None:
        raise AgentChatAdmissionError(
            "Chat Agent binding requires a canonical human actor",
            code="agent_authorization_denied",
        )
    actor = await session.get(User, actor_uuid)
    if actor is None or not bool(getattr(actor, "is_active", True)):
        raise AgentChatAdmissionError(
            "Chat Agent binding actor is unavailable",
            code="agent_authorization_denied",
        )
    actor_role = str(getattr(actor, "role", "") or "") or None
    agent = await session.get(Agent, parse_uuid(canonical_agent_id))
    if agent is None or str(getattr(agent, "state", "")) != "active":
        raise AgentChatAdmissionError(
            "conversation Agent is not active",
            code="agent_authorization_denied",
        )
    project_id = getattr(conversation, "project_id", None)
    if project_id is not None:
        human_read = await ProjectRepository.has_permission(
            session,
            project_id=project_id,
            user_id=actor_uuid,
            permission="read",
        )
        if not human_read:
            raise AgentChatAdmissionError(
                "human project read permission is required",
                code="human_project_read_denied",
            )
        grant = await session.scalar(
            select(AgentProjectGrant).where(
                AgentProjectGrant.agent_id == agent.id,
                AgentProjectGrant.project_id == project_id,
                AgentProjectGrant.state == "active",
            )
        )
        now = datetime.utcnow()
        if (
            grant is None
            or (grant.active_from is not None and grant.active_from > now)
            or (grant.active_until is not None and grant.active_until < now)
            or not bool((grant.permissions or {}).get("read"))
        ):
            raise AgentChatAdmissionError(
                "current Agent project grant is required",
                code="agent_permission_denied",
            )

    binding = await pin_conversation_agent_turn(
        db_session=session,
        session_id=conversation.id,
        agent_id=requested_agent_id,
        agent_revision_id=requested_revision_id,
        actor_user_id=actor_uuid,
        actor_role=actor_role,
        config=config,
    )
    if not getattr(binding, "binding_id", None) or not getattr(binding, "snapshot_hash", None):
        raise AgentChatAdmissionError(
            "conversation Agent binding snapshot is incomplete",
            code="agent_binding_changed",
        )
    resolved_agent = parse_uuid(getattr(binding, "agent_id", None))
    resolved_revision = parse_uuid(getattr(binding, "agent_revision_id", None))
    if resolved_agent is None or resolved_revision is None:
        raise AgentChatAdmissionError(
            "conversation Agent binding snapshot is incomplete",
            code="agent_binding_changed",
        )
    return resolved_agent, resolved_revision, binding


async def _resolve_existing_binding(
    session: Any,
    run: AgentRun,
    *,
    config: Any | None = None,
) -> Any:
    """Resolve an existing run's exact snapshot without creating a new row."""

    from .conversation_agent_service import ConversationAgentService

    metadata = run.run_metadata if isinstance(run.run_metadata, Mapping) else {}
    evidence = metadata.get("agent_binding") if isinstance(metadata, Mapping) else {}
    binding_id = evidence.get("binding_id") if isinstance(evidence, Mapping) else None
    if not run.session_id or not run.agent_id or not run.agent_revision_id:
        raise AgentChatAdmissionError(
            "typed Chat Agent run binding is incomplete",
            code="agent_binding_changed",
        )
    binding = await ConversationAgentService(config=config).resolve_execution_binding(
        session_id=run.session_id,
        agent_id=run.agent_id,
        agent_revision_id=run.agent_revision_id,
        binding_id=binding_id,
        db_session=session,
    )
    if not getattr(binding, "binding_id", None) or not getattr(binding, "snapshot_hash", None):
        raise AgentChatAdmissionError(
            "existing Chat Agent binding snapshot is incomplete",
            code="agent_binding_changed",
        )
    return binding


def _binding_audit_projection(binding: Any) -> dict[str, Any]:
    """Return bounded immutable binding evidence for an AgentRun row."""

    if binding is None:
        return {}
    to_dict = getattr(binding, "to_dict", None)
    raw = to_dict(include_prose=False) if callable(to_dict) else {}
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key in (
        "id",
        "session_id",
        "agent_id",
        "agent_revision_id",
        "revision_version",
        "revision_content_hash",
        "snapshot_hash",
        "route_snapshot",
    ):
        if raw.get(key) not in (None, ""):
            result[key if key != "id" else "binding_id"] = raw.get(key)
    return result


def _activity_timestamp(value: Any) -> str | None:
    """Normalize legacy UTC-naive timestamps for activity consumers."""

    if value in (None, ""):
        return None
    parsed = value if isinstance(value, datetime) else None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
CLOUD_ADVISOR_TOOL_NAME = "consult_cloud_advisor"
CLOUD_ADVISOR_CONTENT_REDACTED_MARKER = "[Cloud Advisor content withheld]"
_CLOUD_ADVISOR_CONTENT_KEYS = frozenset(
    {
        "advisory_text",
        "assistant_response",
        "answer",
        "body",
        "candidate_payload",
        "content",
        "context",
        "context_snapshot",
        "final_payload",
        "input",
        "instructions",
        "message",
        "messages",
        "model_transcript",
        "original_payload",
        "output",
        "output_text",
        "prompt",
        "query",
        "raw_context",
        "raw_prompt",
        "raw_reply",
        "raw_response",
        "reply",
        "response",
        "response_text",
        "text",
        "transcript",
    }
)
_CLOUD_ADVISOR_CONTENT_KEYS_COMPACT = frozenset(
    key.replace("_", "") for key in _CLOUD_ADVISOR_CONTENT_KEYS
)
_CLOUD_ADVISOR_SAFE_EVENT_MESSAGES = frozenset(
    {
        "Agent run queued",
        "Agent run started",
        "Agent run completed",
        "Agent run cancelled",
        "Agent run failed",
    }
)
_TOOL_AUDIT_ARGUMENT_KEYS = frozenset({"tool_args", "arguments", "args"})
_TOOL_AUDIT_RESULT_KEYS = frozenset({"tool_result", "result", "output", "stderr", "error"})
_TOOL_AUDIT_CORRELATION_KEYS = frozenset(
    {
        "tool",
        "tool_name",
        "name",
        "operation_id",
        "tool_call_id",
        "call_id",
        "id",
        "status",
        "state",
        "success",
        "successful",
        "ok",
        "succeeded",
    }
)
_TOOL_AUDIT_ID_KEYS = frozenset(
    {"operation_id", "tool_call_id", "call_id", "id"}
)

# Operations tool calls can carry untrusted external source snapshots and
# application bodies.  These values may be needed transiently by the tool,
# but must not be written to AgentRun arguments/events/transcripts.  Matching
# is deliberately scoped to ``operations_*`` tool names so unrelated tools'
# ordinary ``text``/``body`` fields remain untouched.
OPERATIONS_TOOL_NAME_PREFIX = "operations_"
OPERATIONS_REDACTED_VALUE = "[Operations protected body redacted]"
_OPERATIONS_PROTECTED_FIELD_NAMES = frozenset(
    {
        "source_text",
        "raw_source",
        "source_body",
        "external_body",
        "application_message",
        "message",
        "raw_body",
        "response_body",
        "credential",
        "credential_ref",
        "credentials",
        "password",
        "passphrase",
        "secret",
        "token",
        "access_token",
        "auth_token",
        "session_token",
        "refresh_token",
        "api_key",
        "saml_response",
        "relay_state",
        "authorization",
        "cookie",
        "cookies",
        "browser_data",
        "browser_state",
        "browser_context",
    }
)
_OPERATIONS_PROTECTED_FIELD_NAMES_COMPACT = frozenset(
    name.replace("_", "") for name in _OPERATIONS_PROTECTED_FIELD_NAMES
)
_OPERATIONS_URL_FIELD_NAMES = frozenset({"source_url", "remote_url", "url"})
_OPERATIONS_URL_FIELD_NAMES_COMPACT = frozenset(
    name.replace("_", "") for name in _OPERATIONS_URL_FIELD_NAMES
)


def _is_operations_tool_name(value: Any) -> bool:
    """Return whether a name belongs to the direct Operations tool family."""

    normalized = str(value or "").strip().rsplit(".", 1)[-1].casefold()
    return normalized.startswith(OPERATIONS_TOOL_NAME_PREFIX)


def _operations_protected_field(key: Any) -> bool:
    normalized, compact = _normalized_security_key(key)
    if (
        normalized in _OPERATIONS_PROTECTED_FIELD_NAMES
        or compact in _OPERATIONS_PROTECTED_FIELD_NAMES_COMPACT
    ):
        return True
    # Provider payloads occasionally use a qualified key (for example
    # ``provider_access_token``).  Keep this matching narrow and avoid broad
    # words such as ``status`` or ``summary`` that are safe audit metadata.
    return any(
        marker in normalized
        for marker in (
            "credential",
            "access_token",
            "refresh_token",
            "api_key",
            "password",
            "passphrase",
            "secret",
            "cookie",
            "browser_",
            "raw_source",
            "source_text",
            "source_body",
            "external_body",
            "application_message",
            "response_body",
        )
    )


def _operations_url_field(key: Any) -> bool:
    """Return whether a field is an Operations URL boundary."""

    normalized, compact = _normalized_security_key(key)
    return (
        normalized in _OPERATIONS_URL_FIELD_NAMES
        or compact in _OPERATIONS_URL_FIELD_NAMES_COMPACT
    )


def _durable_operations_url(value: Any) -> Any:
    """Canonicalize a durable Operations URL or replace it with a marker."""

    if value is None:
        return None
    if not isinstance(value, str):
        return OPERATIONS_REDACTED_VALUE
    normalized = sanitize_source_url(value)
    return normalized if normalized is not None else OPERATIONS_REDACTED_VALUE


def _operations_tool_name_from_mapping(value: dict[str, Any]) -> str:
    for key in ("tool", "tool_name", "name"):
        candidate = value.get(key)
        if _is_operations_tool_name(candidate):
            return _clean_tool_name(candidate)
    for nested_key in ("tool_result", "tool_call", "call", "function"):
        nested = value.get(nested_key)
        if isinstance(nested, dict):
            candidate = _operations_tool_name_from_mapping(nested)
            if candidate:
                return candidate
    return ""


def _redact_operations_value(value: Any, *, tool_name: str) -> Any:
    """Recursively redact protected Operations fields in a JSON value."""

    if isinstance(value, list):
        return [
            _redact_operations_value(item, tool_name=tool_name)
            for item in value
        ]
    if isinstance(value, str):
        # Event/tool adapters sometimes serialize a result object one level
        # earlier than the durable boundary.  Decode only JSON objects/arrays;
        # ordinary status/summary strings remain unchanged.
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return value
        if isinstance(parsed, (dict, list)):
            return _redact_operations_value(parsed, tool_name=tool_name)
        return value
    if not isinstance(value, dict):
        return value
    return {
        str(key): (
            OPERATIONS_REDACTED_VALUE
            if _operations_protected_field(key)
            else _durable_operations_url(item)
            if _operations_url_field(key)
            else _redact_operations_value(item, tool_name=tool_name)
        )
        for key, item in value.items()
    }


def _redact_operations_json_arguments(value: Any, *, tool_name: str) -> Any:
    """Redact an Operations function argument object or JSON string."""

    if isinstance(value, (dict, list)):
        safe_value, ok = _safe_audit_redact(value)
        if not ok:
            return {"_redacted": AUDIT_REDACTION_FAILED_MARKER}
        return _redact_operations_value(safe_value, tool_name=tool_name)
    if not isinstance(value, str):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        # Even an unstructured Operations string may contain a labelled or
        # bearer credential, so apply the generic value redactor before
        # returning it.  Structural field projection is only possible for
        # decoded object/array arguments.
        safe_value, ok = _safe_audit_redact(value)
        return safe_value if ok else AUDIT_REDACTION_FAILED_MARKER
    safe_value, ok = _safe_audit_redact(parsed)
    if not ok:
        return AUDIT_REDACTION_FAILED_MARKER
    redacted = _redact_operations_value(safe_value, tool_name=tool_name)
    try:
        return json.dumps(redacted, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return OPERATIONS_REDACTED_VALUE


def set_current_agent_run_id(run_id: str | None) -> Token:
    return _current_agent_run_id.set(str(run_id) if run_id else None)


def reset_current_agent_run_id(token: Token) -> None:
    _current_agent_run_id.reset(token)


def get_current_agent_run_id() -> str | None:
    return _current_agent_run_id.get()


def _jsonable(value: Any) -> Any:
    if value is None:
        return {}
    try:
        return json.loads(json.dumps(value, default=str, ensure_ascii=False))
    except Exception:
        return {"value": str(value)}


def _validated_context_manifest_ref(value: Any) -> dict[str, Any] | None:
    """Return hash-only validated ContextManifest metadata.

    AgentRun records must remain useful for reproducibility without becoming a
    second authority or persisting prompt/context bodies.  The existing
    ContextManifest validator is the only authority for shape/version/hash
    validation; this projection retains only hashes and bounded counters.
    """

    if not isinstance(value, dict):
        return None
    try:
        from ..llm.context_snapshot import validate_context_manifest_metadata

        manifest = validate_context_manifest_metadata(value)
    except Exception:
        manifest = None
    if not isinstance(manifest, dict):
        # Accept an already projected ref (for idempotent retries) but never
        # treat arbitrary provider metadata as a valid manifest.
        manifest_hash = str(value.get("manifest_hash") or "").strip()
        reproducibility = value.get("reproducibility_hashes")
        evidence_hashes = value.get("evidence_hashes")

        def valid_hash(item: Any) -> bool:
            text = str(item or "").strip()
            return (
                len(text) == 71
                and text.startswith("sha256:")
                and all(character in "0123456789abcdef" for character in text[7:].lower())
            )

        if (
            not valid_hash(manifest_hash)
            or not isinstance(reproducibility, dict)
            or not isinstance(evidence_hashes, (list, tuple))
        ):
            return None
        clean_reproducibility = {
            str(key): str(item)
            for key, item in reproducibility.items()
            if str(key) and valid_hash(item)
        }
        clean_evidence = sorted(
            {
                str(item).strip()
                for item in evidence_hashes
                if valid_hash(item)
            }
        )[:512]
        return {
            "manifest_hash": manifest_hash,
            "reproducibility_hashes": clean_reproducibility,
            "evidence_hashes": clean_evidence,
            "evidence_count": len(clean_evidence),
        }

    evidence = manifest.get("evidence")
    evidence_hashes: list[str] = []
    if isinstance(evidence, list):
        for item in evidence:
            if isinstance(item, dict):
                value_hash = item.get("locator_hash") or item.get("ref_hash")
                if value_hash:
                    evidence_hashes.append(str(value_hash))
    reproducibility = manifest.get("reproducibility_hashes")
    if not isinstance(reproducibility, dict):
        reproducibility = {}
    return {
        "schema_version": str(manifest.get("schema_version") or ""),
        "manifest_hash": str(manifest.get("manifest_hash") or ""),
        "reproducibility_hashes": {
            str(key): str(item)
            for key, item in reproducibility.items()
            if str(key) and str(item)
        },
        "evidence_hashes": sorted(set(evidence_hashes))[:512],
        "evidence_count": len(evidence_hashes),
    }


_EXECUTION_MANIFEST_ALLOWED_KEYS = frozenset(
    {
        "schema_version",
        "agent_id",
        "agent_revision_id",
        "agent_revision_version",
        "team_id",
        "execution_profile_id",
        "subagent_id",
        "capabilities",
        "project_id",
        "task_id",
        "work_item_id",
        "persona_id",
        "workspace_access",
        "network_access",
        "run_scope_hash",
        "authority_hash",
        "source",
        "route_snapshot",
    }
)
_EXECUTION_MANIFEST_SECRET_MARKERS = frozenset(
    {"secret", "token", "password", "credential", "api_key", "cookie", "environment", "env"}
)


def _bounded_execution_manifest(value: Any) -> dict[str, Any] | None:
    """Project a trusted execution manifest to bounded, secret-free metadata.

    The manifest is reproducibility evidence, not a second authority.  Only
    known scalar/list fields and hash-like identifiers are retained; arbitrary
    provider configuration, credentials, paths, and environment values are
    dropped.  Invalid input returns ``None`` rather than being persisted.
    """

    if not isinstance(value, dict) or len(value) > 32:
        return None
    clean: dict[str, Any] = {}
    for raw_key, raw_item in value.items():
        key = str(raw_key).strip()
        if key not in _EXECUTION_MANIFEST_ALLOWED_KEYS:
            continue
        if any(marker in key.casefold() for marker in _EXECUTION_MANIFEST_SECRET_MARKERS):
            continue
        if key == "route_snapshot":
            from .agent_model_routing import safe_route_snapshot

            snapshot = safe_route_snapshot(raw_item)
            if not snapshot:
                return None
            clean[key] = snapshot
            continue
        if isinstance(raw_item, (str, int, bool)) or raw_item is None:
            text_value = str(raw_item) if isinstance(raw_item, str) else raw_item
            if isinstance(text_value, str) and (
                len(text_value) > 512
                or "/" in text_value
                or "\\" in text_value
            ):
                return None
            if key in {"workspace_access", "network_access"} and isinstance(text_value, str):
                allowed_values = (
                    {"none", "read", "write"}
                    if key == "workspace_access"
                    else {"none", "organization", "allowlist", "broad"}
                )
                if text_value.casefold() not in allowed_values:
                    return None
            if key in {"run_scope_hash", "authority_hash"} and isinstance(text_value, str):
                if not re.fullmatch(r"[0-9a-f]{64}", text_value.casefold()):
                    return None
            if key in {"agent_id", "agent_revision_id", "project_id", "task_id", "persona_id"} and isinstance(text_value, str):
                if parse_uuid(text_value) is None:
                    return None
            clean[key] = text_value
            continue
        if isinstance(raw_item, (list, tuple)):
            if len(raw_item) > 128 or any(not isinstance(item, (str, int, bool)) for item in raw_item):
                return None
            values = []
            for item in raw_item:
                if isinstance(item, str):
                    if (
                        len(item) > 160
                        or "/" in item
                        or "\\" in item
                        or any(marker in item.casefold() for marker in _EXECUTION_MANIFEST_SECRET_MARKERS)
                    ):
                        return None
                    if key == "capabilities" and item not in AGENT_TEAM_CAPABILITY_CATALOG:
                        return None
                values.append(item)
            clean[key] = list(dict.fromkeys(values))
            continue
        return None
    if not clean:
        return None
    return clean


def _sanitize_context_manifest_fields(value: Any) -> Any:
    """Strip full Manifest bodies from result/event metadata."""

    if isinstance(value, list):
        return [_sanitize_context_manifest_fields(item) for item in value]
    if not isinstance(value, dict):
        return value
    clean = {
        str(key): _sanitize_context_manifest_fields(item)
        for key, item in value.items()
        if key != "context_manifest"
    }
    if "context_manifest" in value:
        ref = _validated_context_manifest_ref(value.get("context_manifest"))
        if ref is not None:
            clean["context_manifest"] = ref
        else:
            clean.pop("context_manifest", None)
    return clean


def conversation_dispatch_fingerprint(payload: Dict[str, Any]) -> str:
    canonical = json.dumps(
        _jsonable(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _redact_sensitive_tool_data(
    value: Any,
    *,
    tool_name: str | None = None,
) -> Any:
    """Remove transient private tool output before durable AgentRun storage."""

    if isinstance(value, list):
        return [
            _redact_sensitive_tool_data(item, tool_name=tool_name)
            for item in value
        ]
    if not isinstance(value, dict):
        return value

    operations_tool_name = (
        _clean_tool_name(tool_name)
        if _is_operations_tool_name(tool_name)
        else _operations_tool_name_from_mapping(value)
    )
    if operations_tool_name:
        # Apply generic credential redaction first so labelled/Bearer secrets
        # in ordinary Operations fields cannot survive, then apply the
        # field-aware projection last to retain its explicit marker and safe
        # IDs/hashes/statuses.
        safe_value, safe_ok = _safe_audit_redact(value)
        if not safe_ok or not isinstance(safe_value, dict):
            return {"_redacted": AUDIT_REDACTION_FAILED_MARKER}
        redacted = _redact_operations_value(
            safe_value,
            tool_name=operations_tool_name,
        )
    else:
        # Generic Director/Cloud/stream payloads are also an audit boundary.
        # Previously only Operations payloads went through the shared display
        # redactor, allowing ``director.raw_reply`` or arbitrary provider
        # metadata to retain labelled/Bearer credentials verbatim.  Apply the
        # same fail-closed projection to every mapping while leaving ordinary
        # non-secret text untouched.
        safe_value, safe_ok = _safe_audit_redact(value)
        if not safe_ok or not isinstance(safe_value, dict):
            return {"_redacted": AUDIT_REDACTION_FAILED_MARKER}
        redacted = safe_value
    tool_result = redacted.get("tool_result")
    tool_name = _clean_tool_name(
        redacted.get("tool")
        or redacted.get("tool_name")
        or redacted.get("name")
    )
    if not tool_name and isinstance(tool_result, dict):
        tool_name = _clean_tool_name(
            tool_result.get("tool")
            or tool_result.get("tool_name")
            or tool_result.get("name")
        )
    if tool_name not in SENSITIVE_TOOL_RESULT_NAMES:
        return redacted

    for key in ("output", "result", "stderr"):
        if key in redacted:
            redacted[key] = SENSITIVE_TOOL_RESULT_MARKER
    if isinstance(tool_result, dict):
        for key in ("output", "result", "stderr"):
            if key in tool_result:
                tool_result[key] = SENSITIVE_TOOL_RESULT_MARKER
    return redacted


def _safe_audit_redact(value: Any) -> tuple[Any, bool]:
    """Secret-redact an audit value, failing closed on redactor errors."""

    try:
        from .outbound_privacy_service import redact_secret_for_local_display

        value_for_redaction = value
        json_string = False
        if isinstance(value, str):
            candidate = value.strip()
            if candidate.startswith(("{", "[")):
                try:
                    value_for_redaction = json.loads(candidate)
                    json_string = isinstance(value_for_redaction, (dict, list))
                except (TypeError, ValueError):
                    value_for_redaction = value
        redacted = redact_secret_for_local_display(value_for_redaction)
        redacted = _redact_nested_json_strings(
            redacted,
            redact_secret_for_local_display,
        )
        # Force a strict JSON boundary here: provider SDK objects and
        # unserializable values must never fall back to their raw repr in
        # durable audit.
        _strict_audit_jsonable(redacted)
        if json_string:
            redacted = json.dumps(
                _strict_audit_jsonable(redacted),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        return redacted, True
    except Exception:
        return AUDIT_REDACTION_FAILED_MARKER, False


def _is_cloud_advisor_run_type(value: Any) -> bool:
    return str(value or "").strip().casefold() in CLOUD_ADVISOR_RUN_TYPES


def _is_cloud_advisor_content_key(value: Any) -> bool:
    normalized, compact = _normalized_security_key(value)
    return (
        normalized in _CLOUD_ADVISOR_CONTENT_KEYS
        or compact in _CLOUD_ADVISOR_CONTENT_KEYS_COMPACT
    )


def _project_cloud_advisor_audit_value(value: Any) -> tuple[Any, bool]:
    """Project one Cloud Advisor value to metadata without retaining bodies.

    Cloud Advisor's ``advisory_text`` is returned to the live parent, not to
    durable AgentRun/audit storage.  This projection recursively removes
    prompt/reply/payload-like fields while retaining the surrounding outcome,
    routing, budget and usage metadata.  It is intentionally independent of
    the privacy gateway: the gateway protects bytes before transport, whereas
    this function protects the local durable audit boundary.
    """

    if isinstance(value, list):
        projected: list[Any] = []
        dropped = False
        for item in value:
            clean, item_dropped = _project_cloud_advisor_audit_value(item)
            projected.append(clean)
            dropped = dropped or item_dropped
        return projected, dropped
    if isinstance(value, tuple):
        projected_items, dropped = _project_cloud_advisor_audit_value(list(value))
        return projected_items, dropped
    if not isinstance(value, dict):
        return value, False

    projected_dict: dict[str, Any] = {}
    dropped = False
    for key, item in value.items():
        key_text = str(key)
        if _is_cloud_advisor_content_key(key_text):
            dropped = True
            continue
        clean, item_dropped = _project_cloud_advisor_audit_value(item)
        projected_dict[key_text] = clean
        dropped = dropped or item_dropped
    return projected_dict, dropped


def sanitize_cloud_advisor_audit_value(value: Any) -> dict[str, Any]:
    """Return a metadata-only projection for Cloud Advisor persistence.

    The helper is public so callers that create an audit record directly can
    apply the same contract as :class:`AgentRunService`.  Redaction failures
    fail closed and never fall back to ``repr(value)``.
    """

    # Tool results/transcripts often arrive as the JSON string emitted by the
    # runtime registry.  Decode only an object-shaped envelope so status and
    # route metadata remain useful; arbitrary text is withheld wholesale.
    candidate = value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = None
        candidate = decoded if isinstance(decoded, dict) else {}
    safe_value, ok = _safe_audit_redact(candidate)
    if not ok or not isinstance(safe_value, dict):
        return {"_redacted": AUDIT_REDACTION_FAILED_MARKER}
    projected, dropped = _project_cloud_advisor_audit_value(safe_value)
    if not isinstance(projected, dict):
        return {"_redacted": AUDIT_REDACTION_FAILED_MARKER}
    if dropped:
        projected["content_redacted"] = True
    try:
        return _strict_audit_jsonable(projected)
    except Exception:
        return {"_redacted": AUDIT_REDACTION_FAILED_MARKER}


def _sanitize_cloud_advisor_event_message(
    event_type: str,
    message: Any,
) -> str | None:
    """Keep lifecycle labels while withholding advisory bodies from events."""

    if message is None:
        return None
    text = str(message)
    if text in _CLOUD_ADVISOR_SAFE_EVENT_MESSAGES:
        return text
    return CLOUD_ADVISOR_CONTENT_REDACTED_MARKER


def _safe_audit_metadata(
    value: Any,
    *,
    run_type: str | None = None,
) -> dict[str, Any]:
    """Normalize metadata before writing it to a durable run/edge row."""

    safe = _redact_sensitive_tool_data(value if isinstance(value, dict) else {})
    if not isinstance(safe, dict):
        return {"_redacted": AUDIT_REDACTION_FAILED_MARKER}
    safe = _sanitize_context_manifest_fields(safe)
    if _is_cloud_advisor_run_type(run_type):
        return sanitize_cloud_advisor_audit_value(safe)
    try:
        return _strict_audit_jsonable(safe)
    except Exception:
        return {"_redacted": AUDIT_REDACTION_FAILED_MARKER}


def _sanitize_serialized_agent_run(value: Any) -> dict[str, Any]:
    """Apply the durable audit contract at the API/read projection too.

    Older rows may predate the write-side sanitizer, and tests/tools can build
    ORM rows directly.  Never return those values verbatim from ``get_run`` or
    ``list_runs``; re-project result, metadata, events and tool evidence on
    read as a final defence.
    """

    payload = dict(value) if isinstance(value, dict) else {}
    run_type = payload.get("run_type")
    if _is_cloud_advisor_run_type(run_type):
        if payload.get("objective"):
            payload["objective"] = CLOUD_ADVISOR_CONTENT_REDACTED_MARKER
        if payload.get("title"):
            payload["title"] = CLOUD_ADVISOR_CONTENT_REDACTED_MARKER

    payload["metadata"] = _safe_audit_metadata(
        payload.get("metadata"),
        run_type=run_type,
    )
    result = _sanitize_context_manifest_fields(
        _redact_sensitive_tool_data(payload.get("result") or {})
    )
    if _is_cloud_advisor_run_type(run_type):
        result = sanitize_cloud_advisor_audit_value(result)
    if isinstance(result, dict) and "assistant_response" in result:
        result["assistant_response"] = sanitize_assistant_display_text(
            result.get("assistant_response")
        )
    payload["result"] = result
    if payload.get("error") is not None:
        payload["error"] = sanitize_durable_error_text(payload["error"])

    events = payload.get("events")
    if isinstance(events, list):
        clean_events: list[dict[str, Any]] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            clean_event = dict(event)
            clean_event["payload"] = _redact_sensitive_tool_data(
                clean_event.get("payload") or {}
            )
            event_tool_name = _clean_tool_name(
                clean_event["payload"].get("tool_name")
                if isinstance(clean_event["payload"], dict)
                else None
            )
            if (
                event_tool_name.casefold() == CLOUD_ADVISOR_TOOL_NAME
            ):
                clean_event["payload"] = sanitize_cloud_advisor_audit_value(
                    clean_event["payload"]
                )
                clean_event["message"] = CLOUD_ADVISOR_CONTENT_REDACTED_MARKER
            elif _is_cloud_advisor_run_type(run_type):
                clean_event["payload"] = sanitize_cloud_advisor_audit_value(
                    clean_event["payload"]
                )
                clean_event["message"] = _sanitize_cloud_advisor_event_message(
                    str(clean_event.get("event_type") or ""),
                    clean_event.get("message"),
                )
            elif clean_event.get("message") is not None:
                clean_event["message"] = sanitize_durable_error_text(
                    clean_event["message"]
                )
            clean_events.append(clean_event)
        payload["events"] = clean_events

    tool_calls = payload.get("tool_calls")
    if isinstance(tool_calls, list):
        clean_tool_calls: list[dict[str, Any]] = []
        for item in tool_calls:
            if not isinstance(item, dict):
                continue
            clean_item = dict(item)
            item_tool_name = _clean_tool_name(
                clean_item.get("tool_name")
                or clean_item.get("name")
                or clean_item.get("tool")
            )
            if item_tool_name.casefold() == CLOUD_ADVISOR_TOOL_NAME:
                clean_item["arguments"] = sanitize_cloud_advisor_audit_value(
                    clean_item.get("arguments") or {}
                )
                clean_item["result"] = sanitize_cloud_advisor_audit_value(
                    clean_item.get("result") or {}
                )
                clean_item["metadata"] = sanitize_cloud_advisor_audit_value(
                    clean_item.get("metadata") or {}
                )
            elif _is_cloud_advisor_run_type(run_type):
                clean_item["arguments"] = sanitize_cloud_advisor_audit_value(
                    clean_item.get("arguments") or {}
                )
                clean_item["result"] = _safe_audit_result(
                    sanitize_cloud_advisor_audit_value(
                        _jsonable(clean_item.get("result"))
                    )
                )
                clean_item["metadata"] = sanitize_cloud_advisor_audit_value(
                    clean_item.get("metadata") or {}
                )
            else:
                clean_item["arguments"] = _safe_audit_arguments(
                    clean_item.get("arguments") or {}
                )
                if clean_item.get("result") is not None:
                    clean_item["result"] = _safe_audit_result(
                        clean_item.get("result")
                    )
                clean_item["metadata"] = _safe_audit_metadata(
                    clean_item.get("metadata")
                )
            clean_tool_calls.append(clean_item)
        payload["tool_calls"] = clean_tool_calls

    edges = payload.get("child_edges")
    if isinstance(edges, list):
        for edge in edges:
            if isinstance(edge, dict):
                edge["metadata"] = _safe_audit_metadata(edge.get("metadata"))
    edges = payload.get("parent_edges")
    if isinstance(edges, list):
        for edge in edges:
            if isinstance(edge, dict):
                edge["metadata"] = _safe_audit_metadata(edge.get("metadata"))

    return payload


def _redact_nested_json_strings(value: Any, redactor) -> Any:
    """Apply secret redaction inside JSON-encoded argument/result strings."""

    if isinstance(value, dict):
        return {
            key: _redact_nested_json_strings(item, redactor)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_nested_json_strings(item, redactor) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_nested_json_strings(item, redactor) for item in value)
    if not isinstance(value, str):
        return value
    candidate = value.strip()
    if not candidate.startswith(("{", "[")):
        return value
    try:
        parsed = json.loads(candidate)
    except (TypeError, ValueError):
        return value
    if not isinstance(parsed, (dict, list)):
        return value
    redacted = redactor(parsed)
    redacted = _redact_nested_json_strings(redacted, redactor)
    return json.dumps(
        _strict_audit_jsonable(redacted),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _strict_audit_jsonable(value: Any) -> Any:
    """Convert only known scalar/container values; reject opaque objects."""

    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not (value == value and abs(value) != float("inf")):
            raise ValueError("non-finite audit value")
        return value
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _strict_audit_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_strict_audit_jsonable(item) for item in value]
    raise TypeError(f"unsupported audit value: {type(value).__name__}")


def _safe_audit_correlation(value: Any) -> str | None:
    """Keep a useful correlation id without preserving token-shaped text."""

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > 256 or any(ord(char) < 32 for char in text):
        return "sha256:" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    redacted, ok = _safe_audit_redact(text)
    if not ok:
        # Keep ordinary operation ids useful even when the optional redactor
        # is unavailable; hash only token-shaped identifiers in this fallback.
        if re.search(
            r"(?i)(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|secret|password|bearer\s+|(?:sk|AIza)[-_A-Za-z0-9]{8,})",
            text,
        ):
            return "sha256:" + hashlib.sha256(
                text.encode("utf-8", "replace")
            ).hexdigest()
        return text
    if not isinstance(redacted, str) or redacted != text:
        return "sha256:" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    return text


def _safe_audit_arguments(value: Any) -> dict[str, Any]:
    """Return a JSON object suitable for ``AgentRunToolCall.arguments``."""

    redacted, ok = _safe_audit_redact(value if isinstance(value, dict) else {})
    if ok and isinstance(redacted, dict):
        try:
            return _strict_audit_jsonable(redacted)
        except Exception:
            pass
    return {"_redacted": AUDIT_REDACTION_FAILED_MARKER}


def _safe_audit_result(value: Any) -> str:
    """Return a bounded, secret-redacted text result for durable audit."""

    redacted, ok = _safe_audit_redact(value)
    if not ok:
        return AUDIT_REDACTION_FAILED_MARKER
    if isinstance(redacted, (dict, list, tuple)):
        try:
            return _clip(
                json.dumps(
                    _strict_audit_jsonable(redacted),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            ) or ""
        except Exception:
            return AUDIT_REDACTION_FAILED_MARKER
    return _clip(redacted) or ""


def sanitize_tool_audit_payload(value: Any) -> dict[str, Any]:
    """Redact tool stream payloads while retaining safe correlation/status.

    The stream event is an audit projection, not an authority channel.  Tool
    names, call/operation identifiers, and status booleans remain available for
    timeline correlation; arguments/results are secret-redacted and become an
    explicit marker if redaction cannot be completed safely.
    """

    if not isinstance(value, dict):
        return {"_redacted": AUDIT_REDACTION_FAILED_MARKER}
    redacted, ok = _safe_audit_redact(value)
    if not ok or not isinstance(redacted, dict):
        # Preserve only bounded correlation/status fields from the raw payload.
        marker: dict[str, Any] = {
            "_redacted": AUDIT_REDACTION_FAILED_MARKER,
        }
        for key in _TOOL_AUDIT_CORRELATION_KEYS:
            if key in value and key != "id":
                marker[key] = (
                    _safe_audit_correlation(value[key])
                    if key in _TOOL_AUDIT_ID_KEYS
                    else value[key]
                )
        for key in ("tool", "tool_name", "name", "id"):
            if key in value:
                marker[key] = (
                    _safe_audit_correlation(value[key])
                    if key in _TOOL_AUDIT_ID_KEYS
                    else value[key]
                )
        for key in _TOOL_AUDIT_ARGUMENT_KEYS | _TOOL_AUDIT_RESULT_KEYS:
            if key in value:
                marker[key] = AUDIT_REDACTION_FAILED_MARKER
        nested_result = value.get("tool_result")
        if isinstance(nested_result, dict):
            nested_marker: dict[str, Any] = {
                "_redacted": AUDIT_REDACTION_FAILED_MARKER,
            }
            for key in _TOOL_AUDIT_CORRELATION_KEYS:
                if key in nested_result:
                    nested_marker[key] = (
                        _safe_audit_correlation(nested_result[key])
                        if key in _TOOL_AUDIT_ID_KEYS
                        else nested_result[key]
                    )
            for key in ("tool", "tool_name", "name", "id"):
                if key in nested_result:
                    nested_marker[key] = (
                        _safe_audit_correlation(nested_result[key])
                        if key in _TOOL_AUDIT_ID_KEYS
                        else nested_result[key]
                    )
            marker["tool_result"] = nested_marker
        return marker

    payload = _strict_audit_jsonable(redacted)
    # Keep correlation and status exactly as emitted; secret redaction only
    # applies to argument/result-bearing fields.
    for key in _TOOL_AUDIT_CORRELATION_KEYS:
        if key in value:
            payload[key] = (
                _safe_audit_correlation(value[key])
                if key in _TOOL_AUDIT_ID_KEYS
                else value[key]
            )
    for key in _TOOL_AUDIT_ARGUMENT_KEYS | _TOOL_AUDIT_RESULT_KEYS:
        if key not in value:
            continue
        raw = value[key]
        if key in _TOOL_AUDIT_ARGUMENT_KEYS:
            safe = _safe_audit_arguments(raw)
            payload[key] = safe if isinstance(raw, dict) else safe
        elif key == "tool_result" and isinstance(raw, dict):
            nested = sanitize_tool_audit_payload(raw)
            payload[key] = nested
        else:
            safe_result, result_ok = _safe_audit_redact(raw)
            payload[key] = safe_result if result_ok else AUDIT_REDACTION_FAILED_MARKER
    return payload


_STREAM_DISPLAY_TEXT_KEYS = frozenset(
    {"text", "content", "delta", "message", "output", "error"}
)


def sanitize_assistant_display_text(value: Any) -> str:
    """Return a secret-redacted assistant string for persistence/UI display."""

    if value is None:
        return ""
    if not isinstance(value, str):
        return AUDIT_REDACTION_FAILED_MARKER
    redacted, ok = _safe_audit_redact(value)
    if not ok or not isinstance(redacted, str):
        return AUDIT_REDACTION_FAILED_MARKER
    # Assistant content is persisted as a full conversation turn.  Stream
    # event payloads apply their own bounded projection; this helper must not
    # truncate long replies after redacting a token near the suffix.
    return redacted


def sanitize_durable_error_text(value: Any) -> str:
    """Return a secret-redacted error suitable for durable AgentRun fields."""

    if value is None:
        return ""
    if not isinstance(value, str):
        return AUDIT_REDACTION_FAILED_MARKER
    redacted, ok = _safe_audit_redact(value)
    if not ok or not isinstance(redacted, str):
        return AUDIT_REDACTION_FAILED_MARKER
    return _clip(redacted, max_chars=5000) or ""


def sanitize_stream_display_payload(
    event_type: str,
    value: Any,
) -> dict[str, Any]:
    """Build one safe stream projection for both audit and websocket paths."""

    normalized_type = str(event_type or "").strip().lower()
    if normalized_type.startswith("stream."):
        normalized_type = normalized_type.split(".", 1)[1]
    if normalized_type in {"tool_start", "tool_end"}:
        return sanitize_tool_audit_payload(value)
    if not isinstance(value, dict):
        return {"_redacted": AUDIT_REDACTION_FAILED_MARKER}

    redacted, ok = _safe_audit_redact(value)
    if not ok or not isinstance(redacted, dict):
        # Reuse the tool sanitizer's fail-closed correlation projection for
        # non-tool progress events, then attach only display markers.
        marker = sanitize_tool_audit_payload(value)
        for key in _STREAM_DISPLAY_TEXT_KEYS:
            if key in value:
                marker[key] = AUDIT_REDACTION_FAILED_MARKER
        return marker
    payload = _strict_audit_jsonable(redacted)
    for key in _TOOL_AUDIT_CORRELATION_KEYS:
        if key in value:
            payload[key] = (
                _safe_audit_correlation(value[key])
                if key in _TOOL_AUDIT_ID_KEYS
                else value[key]
            )
    for key in _STREAM_DISPLAY_TEXT_KEYS:
        if key in value:
            payload[key] = sanitize_assistant_display_text(value[key])
            # Preserve the established event-size bound.  The final
            # persistence/UI path uses sanitize_assistant_display_text
            # directly and therefore remains untruncated.
            if normalized_type != "stream_token":
                text_value = payload[key]
                if isinstance(text_value, str) and len(text_value) > 4000:
                    payload[key] = text_value[:4000].rstrip() + "\n... (truncated)"
    return payload


def _durable_tool_result(tool_name: str, result: Any) -> Any:
    if _clean_tool_name(tool_name) in SENSITIVE_TOOL_RESULT_NAMES:
        return SENSITIVE_TOOL_RESULT_MARKER
    if _clean_tool_name(tool_name).casefold() == CLOUD_ADVISOR_TOOL_NAME:
        # Advisory text is an in-memory parent result; durable tool-call rows
        # keep only status/routing metadata.
        return sanitize_cloud_advisor_audit_value(result)
    if _is_operations_tool_name(tool_name):
        if isinstance(result, (dict, list, str)):
            return _redact_operations_json_arguments(
                result,
                tool_name=_clean_tool_name(tool_name),
            )
    return result


def redact_sensitive_model_transcript(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Redact transient tool bodies from provider transcripts before persistence.

    Provider adapters do not all use the same function-call shape.  Handle
    OpenAI-style ``tool_calls``, legacy ``function_call`` messages, and tool
    result messages while preserving non-sensitive IDs/hashes/status fields.
    """

    tool_names_by_call_id: dict[str, str] = {}
    redacted_messages: list[dict[str, Any]] = []
    for message in messages:
        next_message = dict(message)
        tool_calls = next_message.get("tool_calls")
        if isinstance(tool_calls, list):
            redacted_calls: list[Any] = []
            for call in tool_calls:
                if not isinstance(call, dict):
                    redacted_calls.append(call)
                    continue
                redacted_call = dict(call)
                function = call.get("function")
                function_name = (
                    function.get("name")
                    if isinstance(function, dict)
                    else None
                )
                call_id = str(call.get("id") or call.get("tool_call_id") or "")
                tool_name = _clean_tool_name(
                    function_name or call.get("name") or call.get("tool")
                )
                if call_id and tool_name:
                    tool_names_by_call_id[call_id] = tool_name
                if tool_name.casefold() == CLOUD_ADVISOR_TOOL_NAME:
                    if isinstance(function, dict) and "arguments" in function:
                        redacted_function = dict(function)
                        redacted_function["arguments"] = json.dumps(
                            sanitize_cloud_advisor_audit_value(
                                function.get("arguments")
                            ),
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                        redacted_call["function"] = redacted_function
                    elif "arguments" in redacted_call:
                        redacted_call["arguments"] = sanitize_cloud_advisor_audit_value(
                            redacted_call.get("arguments")
                        )
                elif _is_operations_tool_name(tool_name):
                    if isinstance(function, dict):
                        redacted_function = dict(function)
                        if "arguments" in redacted_function:
                            redacted_function["arguments"] = _redact_operations_json_arguments(
                                redacted_function["arguments"],
                                tool_name=tool_name,
                            )
                        redacted_call["function"] = redacted_function
                    elif "arguments" in redacted_call:
                        redacted_call["arguments"] = _redact_operations_json_arguments(
                            redacted_call["arguments"],
                            tool_name=tool_name,
                        )
                redacted_calls.append(redacted_call)
            next_message["tool_calls"] = redacted_calls

        # Legacy OpenAI function-call shape: {function_call: {name,
        # arguments}}.  It has no call id, so the function name is authoritative.
        function_call = next_message.get("function_call")
        if isinstance(function_call, dict):
            function_name = _clean_tool_name(
                function_call.get("name")
                or function_call.get("tool")
            )
            if _is_operations_tool_name(function_name) and "arguments" in function_call:
                redacted_function_call = dict(function_call)
                redacted_function_call["arguments"] = _redact_operations_json_arguments(
                    redacted_function_call["arguments"],
                    tool_name=function_name,
                )
                next_message["function_call"] = redacted_function_call

        if next_message.get("role") == "tool":
            call_id = str(next_message.get("tool_call_id") or "")
            tool_name = _clean_tool_name(
                next_message.get("name")
                or next_message.get("tool")
                or tool_names_by_call_id.get(call_id)
            )
            if tool_name in SENSITIVE_TOOL_RESULT_NAMES:
                next_message["content"] = SENSITIVE_TOOL_RESULT_MARKER
                for key in ("output", "result"):
                    if key in next_message:
                        next_message[key] = SENSITIVE_TOOL_RESULT_MARKER
            elif tool_name.casefold() == CLOUD_ADVISOR_TOOL_NAME:
                projected = sanitize_cloud_advisor_audit_value(
                    next_message.get("content")
                )
                next_message["content"] = json.dumps(
                    projected,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                for key in ("output", "result"):
                    if key in next_message:
                        next_message[key] = projected
            elif _is_operations_tool_name(tool_name):
                # Tool results are often JSON strings in ``content`` but some
                # providers preserve the decoded object under ``result`` or
                # ``output``.  Apply the same field-aware redaction to each.
                if "content" in next_message:
                    next_message["content"] = _redact_operations_json_arguments(
                        next_message["content"],
                        tool_name=tool_name,
                    )
                for key in ("output", "result", "arguments", "args"):
                    if key in next_message:
                        next_message[key] = _redact_operations_json_arguments(
                            next_message[key],
                            tool_name=tool_name,
                        )
        elif next_message.get("role") == "function":
            tool_name = _clean_tool_name(next_message.get("name"))
            if tool_name.casefold() == CLOUD_ADVISOR_TOOL_NAME:
                next_message["content"] = json.dumps(
                    sanitize_cloud_advisor_audit_value(next_message.get("content")),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            elif _is_operations_tool_name(tool_name):
                for key in ("content", "output", "result", "arguments", "args"):
                    if key in next_message:
                        next_message[key] = _redact_operations_json_arguments(
                            next_message[key],
                            tool_name=tool_name,
                        )
        redacted_messages.append(next_message)
    return redacted_messages


def redact_sensitive_chat_metadata(
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Apply transcript redaction at the final conversation persistence boundary."""

    redacted = dict(metadata)
    transcript = redacted.get("model_transcript")
    if isinstance(transcript, list):
        redacted["model_transcript"] = redact_sensitive_model_transcript(
            [
                dict(message)
                for message in transcript
                if isinstance(message, dict)
            ]
        )
    return redacted


def _clip(text: Any, max_chars: int = 20000) -> str | None:
    if text is None:
        return None
    value = str(text)
    if len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip() + "\n... (truncated)"


def _canonical_tool_arguments(value: Any) -> str:
    """Return a stable representation for approved-action argument matching."""

    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def canonical_audit_digest(value: Any) -> str:
    """Hash canonical raw tool data without retaining the raw value."""

    return "sha256:" + hashlib.sha256(
        _canonical_tool_arguments(value).encode("utf-8")
    ).hexdigest()


def _is_audit_digest(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 71 and text.startswith("sha256:") and all(
        character in "0123456789abcdef" for character in text[7:]
    )


def _approved_receipt_metadata(
    value: Any,
    *,
    atomic_state: str,
) -> dict[str, Any]:
    """Build the bounded metadata carried by an atomic approved receipt."""

    raw = value if isinstance(value, dict) else {}
    clean = {
        str(key): _jsonable(item)
        for key, item in raw.items()
        if str(key) in {
            "source",
            "plan_id",
            "plan_revision",
            "action_index",
            "action_digest",
            "arguments_digest",
            "result_digest",
        }
    }
    # The source and state are protocol fields, never caller-controlled.
    clean["source"] = "approved_plan_executor"
    clean["atomic_state"] = str(atomic_state)
    clean["atomic"] = True
    return _jsonable(_redact_sensitive_tool_data(clean))


def _approved_receipt_expected_metadata(value: Any) -> dict[str, Any]:
    """Normalize expected metadata for exact source/state validation."""

    raw = value if isinstance(value, dict) else {}
    expected: dict[str, Any] = {
        "source": "approved_plan_executor",
        "atomic": True,
    }
    for key in (
        "plan_id",
        "plan_revision",
        "action_index",
        "action_digest",
        "arguments_digest",
    ):
        if key in raw:
            expected[key] = _jsonable(raw[key])
    return expected


def approved_mutation_receipt_result(receipt: AgentRunToolCall | dict[str, Any]) -> Any:
    """Decode a prior approved receipt for a tool-level idempotent replay."""

    raw = receipt.result if isinstance(receipt, AgentRunToolCall) else receipt.get("result")
    if raw is None:
        return ""
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError):
        return str(raw)


def fold_cancelled_chat_snapshot(
    events: list[AgentRunEvent],
) -> dict[str, Any]:
    """Fold the latest streamed attempt into a durable cancelled-turn snapshot."""

    content_parts: list[str] = []
    tool_results: list[dict[str, Any]] = []
    tool_result_keys: set[str] = set()
    stream_start_sequence: int | None = None
    last_stream_token_sequence: int | None = None

    for event in sorted(events, key=lambda item: int(item.sequence or 0)):
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.event_type == "stream.stream_start":
            content_parts = []
            tool_results = []
            tool_result_keys = set()
            stream_start_sequence = int(event.sequence or 0)
            last_stream_token_sequence = None
            continue
        if event.event_type == "stream.assistant_text":
            # Native tool-call rounds publish their visible intermediate text
            # through assistant_text.  It is intentionally separate from the
            # final stream_token path, but it still belongs in a cancelled
            # turn's latest partial draft.
            content = payload.get("text")
            if not isinstance(content, str) or not content:
                content = payload.get("content")
            if isinstance(content, str) and content:
                content_parts.append(content)
            continue
        if event.event_type == "stream.stream_token":
            content = payload.get("content")
            if isinstance(content, str) and content:
                content_parts.append(content)
                last_stream_token_sequence = int(event.sequence or 0)
            continue
        if event.event_type == "stream.stream_end":
            content = payload.get("content")
            if isinstance(content, str) and content:
                content_parts = [content]
            continue
        if event.event_type != "stream.tool_end":
            continue
        tool_result = payload.get("tool_result")
        if isinstance(tool_result, dict):
            correlations = {
                str(source.get(field) or "").strip()
                for source in (payload, tool_result)
                for field in ("operation_id", "tool_call_id", "call_id")
                if str(source.get(field) or "").strip()
            }
            if correlations and any(
                f"id:{correlation}" in tool_result_keys
                for correlation in correlations
            ):
                continue
            tool_result_keys.update(
                f"id:{correlation}" for correlation in correlations
            )
            tool_results.append(
                {
                    str(key): _jsonable(value)
                    for key, value in tool_result.items()
                    if key != "tool_call_id"
                }
            )

    return {
        "content": "".join(content_parts),
        "tool_results": tool_results,
        "stream_start_sequence": stream_start_sequence,
        "last_stream_token_sequence": last_stream_token_sequence,
    }


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _clean_tool_name(value: Any) -> str:
    return str(value or "").strip()


def _looks_like_shell_command(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    lower = text.lower()
    shell_markers = (
        "powershell.exe",
        "\\pwsh.exe",
        "/pwsh",
        "cmd.exe",
        " -command ",
        " -command'",
        " -command\"",
        " /c ",
        " -c ",
    )
    return any(marker in lower for marker in shell_markers)


def _normalize_tool_name(value: Any) -> str:
    clean_name = _clean_tool_name(value)
    if _looks_like_shell_command(clean_name):
        return "shell_command"
    return clean_name


def _payload_shell_command(payload: dict[str, Any]) -> str:
    tool_args = payload.get("tool_args")
    if isinstance(tool_args, dict):
        command = tool_args.get("command")
        if isinstance(command, str) and _looks_like_shell_command(command):
            return command.strip()

    tool_result = payload.get("tool_result")
    if isinstance(tool_result, dict):
        arguments = tool_result.get("arguments")
        if isinstance(arguments, dict):
            command = arguments.get("command")
            if isinstance(command, str) and _looks_like_shell_command(command):
                return command.strip()
    return ""


def _payload_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _model_text(payload: dict[str, Any], *keys: str) -> str:
    value = _payload_text(payload, *keys)
    if value.strip().lower() == "default":
        return ""
    return value


def _humanize_key(value: str) -> str:
    return value.replace("_", " ").strip() or "ツール"


TOOL_OPERATION_LABELS = {
    "web_search": "Webを検索",
    "search_web": "Webを検索",
    "shell_command": "コマンドを実行",
    "get_weather": "天気を確認",
    "get_current_time": "現在時刻を確認",
    "calculate": "計算を実行",
    "create_task": "タスクを作成",
    "update_task": "タスクを更新",
    "list_tasks": "タスクを確認",
    "list_project_information": "案件情報を確認",
    "get_project_context": "案件コンテキストを確認",
    "list_record_tables": "台帳を確認",
    "read_file": "ファイルを読み取り",
    "write_file": "ファイルを編集",
    "execute_code": "コードを実行",
    "generate_image": "画像を生成",
}


def _tool_operation_label(tool_name: str, actor_label: str | None = None) -> str:
    return TOOL_OPERATION_LABELS.get(
        tool_name,
        f"{actor_label or _humanize_key(tool_name)}を実行",
    )


def _event_operation_key(event: AgentRunEvent) -> str:
    payload = event.payload if isinstance(event.payload, dict) else {}
    return canonical_event_operation_key(payload)


def _event_tool_arguments(payload: dict[str, Any]) -> dict[str, Any]:
    tool_name = _event_tool_name("", payload)
    for key in ("tool_args", "arguments", "args"):
        value = payload.get(key)
        if isinstance(value, dict):
            normalized = _jsonable(value)
            if _is_operations_tool_name(tool_name):
                return _redact_operations_value(
                    normalized,
                    tool_name=tool_name,
                )
            return normalized
    tool_result = payload.get("tool_result")
    if isinstance(tool_result, dict):
        for key in ("arguments", "args"):
            value = tool_result.get(key)
            if isinstance(value, dict):
                normalized = _jsonable(value)
                if _is_operations_tool_name(tool_name):
                    return _redact_operations_value(
                        normalized,
                        tool_name=tool_name,
                    )
                return normalized
    return {}


def _event_tool_result(payload: dict[str, Any]) -> str | None:
    tool_result = payload.get("tool_result")
    if not isinstance(tool_result, dict):
        return None
    for key in ("output", "result"):
        value = tool_result.get(key)
        if value is not None:
            tool_name = _event_tool_name("", payload)
            if _is_operations_tool_name(tool_name):
                value = _durable_tool_result(tool_name, value)
            return _clip(value)
    return None


_TOOL_SUCCESS_STATUSES = frozenset(
    {"success", "succeeded", "complete", "completed", "done", "ok"}
)
_TOOL_FAILURE_STATUSES = frozenset(
    {"failed", "failure", "error", "errored", "cancelled", "canceled", "aborted"}
)


def _tool_status_value(value: Any) -> bool | None:
    """Normalize the status variants emitted by provider/tool adapters."""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 0:
            return False
        if value == 1:
            return True
    normalized = str(value or "").strip().lower()
    if normalized in _TOOL_SUCCESS_STATUSES or normalized in {"true", "yes", "1"}:
        return True
    if normalized in _TOOL_FAILURE_STATUSES or normalized in {"false", "no", "0"}:
        return False
    return None


def _tool_payload_outcome(
    payload: dict[str, Any] | None,
    *,
    event_status: Any = None,
    completed: bool = False,
) -> tuple[bool | None, str | None, str | None, bool]:
    """Return ``(success, error, result, explicit)`` for one tool payload.

    ``explicit`` distinguishes a provider's status/error from the historical
    shape where a ``stream.tool_end`` merely meant "the operation ended".
    This lets a durable ToolCall remain authoritative when old end events have
    no success field, while still honoring a concrete tool error/status.
    """

    source = payload if isinstance(payload, dict) else {}
    tool_result = source.get("tool_result")
    result_payload = tool_result if isinstance(tool_result, dict) else source
    result = None
    for key in ("output", "result"):
        value = result_payload.get(key)
        if value is not None:
            result = _clip(value)
            break

    error = _event_tool_error(source)
    if error:
        return False, error, result, True

    status_values: list[Any] = [event_status, source.get("status"), source.get("state")]
    for candidate in (tool_result,):
        if isinstance(candidate, dict):
            status_values.extend([candidate.get("status"), candidate.get("state")])
    for value in status_values:
        status = _tool_status_value(value)
        if status is not None:
            return status, None, result, True

    bool_values: list[Any] = []
    for candidate in (source, tool_result):
        if not isinstance(candidate, dict):
            continue
        bool_values.extend(
            candidate.get(key)
            for key in ("success", "successful", "ok", "succeeded")
            if key in candidate
        )
    for value in bool_values:
        status = _tool_status_value(value)
        if status is not None:
            return status, None, result, True

    # A non-zero process/HTTP exit code is a tool failure even when the
    # adapter omitted the separate ``error`` field.
    for candidate in (source, tool_result):
        if not isinstance(candidate, dict):
            continue
        for key in ("exit_code", "returncode", "exit_status"):
            if key not in candidate or candidate[key] is None:
                continue
            try:
                code = int(candidate[key])
            except (TypeError, ValueError):
                continue
            return (code == 0), None, result, True

    if completed:
        # Legacy ``stream.tool_end`` records had no status but did indicate a
        # completed invocation.  Preserve that successful-history behavior.
        return True, None, result, False
    return None, None, result, False


def _event_tool_error(payload: dict[str, Any]) -> str | None:
    tool_result = payload.get("tool_result")
    if isinstance(tool_result, dict) and tool_result.get("error"):
        return _clip(tool_result["error"], max_chars=4000)
    if payload.get("error"):
        return _clip(payload["error"], max_chars=4000)
    return None


def _event_tool_name(event_type: str, payload: dict[str, Any]) -> str:
    tool_name = _clean_tool_name(
        payload.get("tool")
        or payload.get("tool_name")
        or payload.get("name")
    )
    if tool_name:
        return tool_name

    tool_result = payload.get("tool_result")
    if isinstance(tool_result, dict):
        return _clean_tool_name(
            tool_result.get("tool")
            or tool_result.get("tool_name")
            or tool_result.get("name")
        )

    if event_type.startswith("tool."):
        return _clean_tool_name(payload.get("tool_name"))
    return ""


def _actor_for_tool(tool_name: str) -> dict[str, str | None]:
    clean_name = _normalize_tool_name(tool_name)
    legacy = _LEGACY_HISTORY_TOOL_ACTORS.get(clean_name)
    if legacy:
        actor_type, actor_key, actor_label = legacy
        return {
            "actor_type": actor_type,
            "actor_key": actor_key,
            "actor_label": actor_label,
        }
    if clean_name in _SPOTIFY_TOOL_NAMES:
        return {
            "actor_type": "integration",
            "actor_key": "spotify",
            "actor_label": "Spotify連携",
        }
    subagent_id = AGENT_TEAM_TOOL_SUBAGENTS.get(clean_name)
    if subagent_id:
        return {
            "actor_type": "agent_team",
            "actor_key": subagent_id,
            "actor_label": AGENT_TEAM_SUBAGENT_CATALOG.get(subagent_id, {}).get("name", subagent_id),
        }
    if clean_name:
        return {
            "actor_type": "tool",
            "actor_key": clean_name,
            "actor_label": DIRECT_TOOL_LABELS.get(
                clean_name,
                _humanize_key(clean_name),
            ),
        }
    return {
        "actor_type": "assistant",
        "actor_key": "main",
        "actor_label": "メインエージェント",
    }


def _actor_for_event(run: AgentRun, event: AgentRunEvent) -> dict[str, str | None]:
    payload = event.payload if isinstance(event.payload, dict) else {}
    actor_key = _clean_tool_name(
        payload.get("agent_instance_key")
        or payload.get("actor_instance_key")
        or payload.get("subagent_id")
        or payload.get("agent_member_key")
        or payload.get("actor_key")
    )
    if actor_key:
        return {
            "actor_type": str(payload.get("actor_type") or "agent_team"),
            "actor_key": actor_key,
            "actor_label": str(
                payload.get("agent_label")
                or payload.get("actor_label")
                or AGENT_TEAM_SUBAGENT_CATALOG.get(actor_key, {}).get("name", actor_key)
            ),
        }

    tool_name = _event_tool_name(event.event_type, payload)
    if tool_name:
        return _actor_for_tool(tool_name)

    if event.event_type.startswith("run.queued"):
        return {
            "actor_type": "system",
            "actor_key": "system",
            "actor_label": "システム",
        }
    return {
        "actor_type": "assistant",
        "actor_key": "main",
        "actor_label": "メインエージェント",
    }


def _event_action(event_type: str, tool_label: str | None = None) -> str:
    if event_type == "run.queued":
        return "実行をキューに追加"
    if event_type == "run.started":
        return "応答生成を開始"
    if event_type == "run.succeeded":
        return "応答生成を完了"
    if event_type == "run.failed":
        return "応答生成に失敗"
    if event_type == "run.cancelled":
        return "応答生成を停止"
    if event_type.endswith(".ignored"):
        return "状態更新を無視"
    if event_type == "stream.stream_start":
        return "ストリームを開始"
    if event_type == "stream.tool_start":
        return f"{tool_label or 'ツール'}を実行開始"
    if event_type == "stream.tool_end":
        return f"{tool_label or 'ツール'}の実行完了"
    if event_type == "stream.stream_end":
        return "ストリームを完了"
    if event_type == "stream.stream_cancelled":
        return "ストリームを停止"
    if event_type == "stream.assistant_text":
        return "途中経過"
    if event_type == "stream.thinking":
        return "思考"
    if event_type == "stream.reasoning_progress":
        return "推論状況を更新"
    if event_type == "stream.status_update":
        return "進捗を更新"
    if event_type == "stream.steering_update":
        return "追加指示を受信"
    if event_type == "agent_team.instance_started":
        return f"{tool_label or 'エージェント'}を実行開始"
    if event_type == "agent_team.instance_succeeded":
        return f"{tool_label or 'エージェント'}の実行完了"
    if event_type == "agent_team.instance_failed":
        return f"{tool_label or 'エージェント'}の実行に失敗"
    if event_type == "director.round_started":
        return "Directorへ送信"
    if event_type in {"director.raw_reply", "director.reply_received"}:
        return "Directorから受信"
    if event_type == "director.final_answer":
        return "Directorが最終回答を作成"
    if event_type == "director.round_limit_reached":
        return "Director往復上限で停止"
    if event_type == "director.session_save_failed":
        return "Director会話情報の保存に失敗"
    if event_type == "director.needs_human":
        return "ChatGPT接続の確認が必要"
    if event_type == "director.busy":
        return "ChatGPT接続が使用中"
    if event_type == "director.operator_started":
        return "Operatorを実行開始"
    if event_type == "director.operator_succeeded":
        return "Operatorの実行完了"
    if event_type == "director.operator_failed":
        return "Operatorの実行に失敗"
    if event_type == "tool.end":
        return f"{tool_label or 'ツール'}の結果を記録"
    if event_type == "tool.failed":
        return f"{tool_label or 'ツール'}の失敗を記録"
    return _humanize_key(event_type)


def _timeline_event_display_status(event: AgentRunEvent) -> str | None:
    if event.event_type == "run.queued":
        return "recorded"
    if event.event_type == "run.started":
        return "started"
    if event.status in {"queued", "running", "tool"}:
        if event.event_type.endswith("_start") or event.event_type.endswith(".started"):
            return "started"
        return "recorded"
    return event.status


def _timeline_event_message(
    message: str | None,
    *,
    raw_tool_name: str,
    tool_name: str,
) -> str | None:
    if (
        message
        and tool_name == "shell_command"
        and raw_tool_name
        and raw_tool_name in message
    ):
        return message.replace(raw_tool_name, tool_name)
    return message


def _timeline_event_visibility(
    event_type: str,
    payload: dict[str, Any],
) -> str:
    """Separate concise user-facing progress from the complete audit trail."""

    if event_type == "stream.assistant_text":
        return "normal"
    if event_type == "stream.thinking":
        kind = str(payload.get("kind") or "").strip().lower()
        if (
            kind in {"summary", "reasoning_summary"}
            or payload.get("is_summary") is True
            or payload.get("reasoning_summary") is True
        ):
            return "normal"
        return "audit"

    explicit = str(
        payload.get("visibility")
        or payload.get("display_kind")
        or ""
    ).strip().lower()
    if explicit in {"normal", "audit"}:
        return explicit
    if payload.get("user_visible") is True:
        return "normal"

    # Only normalized, actionable summaries are promoted.  Provider JSONL,
    # turn/session/item lifecycle and unknown CLI statuses remain audit-only.
    if event_type in {
        "director.needs_human",
        "director.busy",
        "director.operator_started",
        "director.operator_succeeded",
        "director.operator_failed",
        "director.final_answer",
        "stream.agentic_review",
    }:
        return "normal"
    if event_type.startswith("agent_team.instance_"):
        return "normal"
    return "audit"


def _timeline_event_item(run: AgentRun, event: AgentRunEvent) -> dict[str, Any]:
    payload = event.payload if isinstance(event.payload, dict) else {}
    if event.event_type == "stream.thinking" and (
        str(payload.get("kind") or "").strip().lower() == "reasoning_summary"
        or payload.get("is_summary") is True
        or payload.get("reasoning_summary") is True
    ):
        payload = {**payload, "kind": "summary"}
    raw_tool_name = _event_tool_name(event.event_type, payload)
    tool_name = _normalize_tool_name(raw_tool_name)
    raw_display_tool_name = (
        raw_tool_name
        if raw_tool_name and raw_tool_name != tool_name
        else _payload_shell_command(payload)
    )
    actor = _actor_for_event(run, event)
    provider = _payload_text(payload, "provider", "agent_provider", "model_provider")
    model = _model_text(payload, "model", "agent_model", "model_name")
    if actor["actor_key"] == "main":
        provider = provider or run.provider or ""
        model = model or _model_text({"model": run.model}, "model")
    tool_label = (
        str(actor["actor_label"])
        if tool_name and actor.get("actor_label")
        else None
    )
    if event.event_type.startswith("agent_team.") and actor.get("actor_label"):
        tool_label = str(actor["actor_label"])
    item: dict[str, Any] = {
        "id": f"event:{event.id}",
        "source": "event",
        "run_id": str(event.run_id),
        "sequence": event.sequence,
        "event_type": event.event_type,
        "visibility": _timeline_event_visibility(event.event_type, payload),
        "status": event.status,
        "display_status": _timeline_event_display_status(event),
        "actor_type": actor["actor_type"],
        "actor_key": actor["actor_key"],
        "actor_label": actor["actor_label"],
        "provider": provider or None,
        "model": model or None,
        "mode": _payload_text(
            payload, "mode", "model_mode", "reasoning_effort", "effort"
        ) or None,
        "team_id": _payload_text(payload, "team_id") or None,
        "subagent_id": _payload_text(payload, "subagent_id", "agent_member_key") or None,
        "llm_profile_id": _payload_text(payload, "llm_profile_id") or None,
        "routing_profile": _payload_text(
            payload, "routing_profile", "routing_profile_id"
        )
        or None,
        "pool": _payload_text(payload, "pool", "pool_id") or None,
        "credential_profile": _payload_text(
            payload, "credential_profile", "credential_profile_id"
        )
        or None,
        "candidate": _payload_text(payload, "candidate", "candidate_id") or None,
        "quota_pool_ids": list(payload.get("quota_pool_ids") or []),
        "fallback_count": int(payload.get("fallback_count") or 0),
        "action": _event_action(event.event_type, tool_label),
        "message": _timeline_event_message(
            event.message,
            raw_tool_name=raw_display_tool_name,
            tool_name=tool_name,
        ),
        "tool_name": tool_name or None,
        "raw_tool_name": (
            raw_display_tool_name if raw_display_tool_name else None
        ),
        "tool_call_id": _event_operation_key(event) or None,
        "arguments": _event_tool_arguments(payload),
        "result": _event_tool_result(payload),
        "result_preview": _clip(_event_tool_result(payload), max_chars=1200),
        "error": _event_tool_error(payload),
        "payload": _jsonable(payload),
        "created_at": _dt(event.created_at),
    }
    return item


def _timeline_tool_call_item(
    run: AgentRun,
    tool_call: AgentRunToolCall,
    *,
    operation_id: str | None = None,
    operation_started_at: datetime | None = None,
    operation_ended_at: datetime | None = None,
    operation_end_payload: dict[str, Any] | None = None,
    operation_end_status: Any = None,
) -> dict[str, Any]:
    raw_tool_name = _clean_tool_name(tool_call.tool_name)
    tool_name = _normalize_tool_name(raw_tool_name)
    actor = _actor_for_tool(tool_name)
    metadata = tool_call.result_metadata if isinstance(tool_call.result_metadata, dict) else {}
    arguments = tool_call.arguments or {}
    if tool_name == "shell_command" and raw_tool_name != tool_name:
        arguments = dict(arguments)
        arguments.setdefault("command", raw_tool_name)
    end_success, end_error, end_result, end_explicit = _tool_payload_outcome(
        operation_end_payload,
        event_status=operation_end_status,
        completed=operation_end_payload is not None,
    )
    durable_success, durable_error, durable_result, durable_explicit = _tool_payload_outcome(
        metadata,
    )
    # Concrete tool evidence wins. A bare legacy end event defers to the
    # durable ToolCall, and the parent run status is deliberately ignored.
    if end_error or (end_explicit and end_success is False):
        success = False
        error = end_error
    elif durable_error or (durable_explicit and durable_success is False):
        success = False
        error = durable_error
    elif end_explicit:
        success = bool(end_success)
        error = None
    elif durable_explicit:
        success = bool(durable_success)
        error = None
    else:
        success = bool(tool_call.success)
        error = None
    # Keep the durable audit result as the primary display value. Older
    # streams may contain a truncated/provider-specific end preview.
    result = _clip(tool_call.result) or durable_result or end_result
    started_at = tool_call.started_at or operation_started_at
    ended_at = tool_call.ended_at or operation_ended_at
    duration_ms = tool_call.duration_ms
    if duration_ms is None and started_at and ended_at and ended_at >= started_at:
        duration_ms = int((ended_at - started_at).total_seconds() * 1000)
    return {
        "id": operation_id or f"tool:{tool_call.id}",
        "source": "tool_call",
        "run_id": str(tool_call.run_id),
        "event_id": str(tool_call.event_id) if tool_call.event_id else None,
        "event_type": "tool_call",
        "visibility": "normal",
        "status": "succeeded" if success else "failed",
        "display_status": "succeeded" if success else "failed",
        "actor_type": actor["actor_type"],
        "actor_key": actor["actor_key"],
        "actor_label": actor["actor_label"],
        "provider": _payload_text(
            metadata,
            "provider",
            "agent_provider",
            "model_provider",
        )
        or run.provider
        or None,
        "model": _model_text(metadata, "model", "agent_model", "model_name")
        or _model_text({"model": run.model}, "model")
        or None,
        "mode": _payload_text(metadata, "mode", "model_mode", "reasoning_effort") or None,
        "team_id": _payload_text(metadata, "team_id") or None,
        "subagent_id": _payload_text(metadata, "subagent_id", "agent_member_key") or None,
        "llm_profile_id": _payload_text(metadata, "llm_profile_id") or None,
        "action": _tool_operation_label(tool_name, actor.get("actor_label")),
        "message": tool_name,
        "tool_name": tool_name,
        "raw_tool_name": raw_tool_name if raw_tool_name != tool_name else None,
        "tool_call_id": tool_call.tool_call_id,
        "arguments": arguments,
        "result": result,
        "result_preview": _clip(result, max_chars=1200),
        "error": error,
        "success": success,
        "mutation_confirmed": bool(tool_call.mutation_confirmed),
        "duration_ms": duration_ms,
        "payload": metadata,
        "created_at": _dt(tool_call.created_at),
        "started_at": _dt(started_at),
        "ended_at": _dt(ended_at),
    }


def build_agent_run_timeline(run: AgentRun) -> list[dict[str, Any]]:
    """Build UI work records, correlating lifecycle events into one operation."""

    items: list[tuple[datetime, int, dict[str, Any]]] = []
    events = sorted(
        list(getattr(run, "events", []) or []),
        key=lambda event: (event.created_at or datetime.min, int(event.sequence or 0)),
    )
    tool_calls = sorted(
        list(getattr(run, "tool_calls", []) or []),
        key=lambda call: call.created_at or call.started_at or datetime.min,
    )

    tool_operations: list[dict[str, Any]] = []
    open_tool_operations: dict[str, list[dict[str, Any]]] = {}
    agent_operations: list[dict[str, Any]] = []
    open_agent_operations: dict[str, list[dict[str, Any]]] = {}
    interrupted_status = run.status if run.status in {"failed", "cancelled"} else None

    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        event_type = event.event_type
        created_at = event.created_at or datetime.min

        if event_type.startswith("agent_team.instance_"):
            actor = _actor_for_event(run, event)
            stable_key = _event_operation_key(event) or str(actor.get("actor_key") or "")
            if event_type == "agent_team.instance_started":
                operation = {"start": event, "end": None, "key": stable_key}
                agent_operations.append(operation)
                open_agent_operations.setdefault(stable_key, []).append(operation)
            else:
                queue = open_agent_operations.get(stable_key, [])
                operation = queue.pop(0) if queue else {
                    "start": None,
                    "end": None,
                    "key": stable_key,
                }
                if not queue:
                    open_agent_operations.pop(stable_key, None)
                if operation not in agent_operations:
                    agent_operations.append(operation)
                operation["end"] = event
            continue

        if event_type in {"stream.tool_start", "stream.tool_end"}:
            raw_tool_name = _event_tool_name(event_type, payload)
            tool_name = _normalize_tool_name(raw_tool_name)
            stable_id = _event_operation_key(event)
            queue_key = f"id:{stable_id}" if stable_id else f"name:{tool_name}"
            if event_type == "stream.tool_start":
                signature = (
                    raw_tool_name
                    if _looks_like_shell_command(raw_tool_name)
                    else _payload_shell_command(payload)
                    or json.dumps(
                        _event_tool_arguments(payload),
                        sort_keys=True,
                        ensure_ascii=False,
                    )
                )
                current_queue = open_tool_operations.get(queue_key, [])
                if not stable_id and current_queue:
                    previous = current_queue[-1]
                    previous_start = previous.get("start")
                    previous_at = previous_start.created_at if previous_start else None
                    is_immediate_duplicate = (
                        previous.get("signature") == signature
                        and previous_at is not None
                        and event.created_at is not None
                        and 0
                        <= (event.created_at - previous_at).total_seconds()
                        <= 0.05
                    )
                    if is_immediate_duplicate:
                        continue
                operation = {
                    "start": event,
                    "end": None,
                    "key": queue_key,
                    "tool_name": tool_name,
                    "signature": signature,
                    "used": False,
                }
                tool_operations.append(operation)
                open_tool_operations.setdefault(queue_key, []).append(operation)
            else:
                queue = open_tool_operations.get(queue_key, [])
                operation = None
                signature = (
                    raw_tool_name
                    if _looks_like_shell_command(raw_tool_name)
                    else _payload_shell_command(payload)
                    or json.dumps(
                        _event_tool_arguments(payload),
                        sort_keys=True,
                        ensure_ascii=False,
                    )
                )
                if not stable_id:
                    matching_index = next(
                        (
                            index
                            for index, candidate in enumerate(queue)
                            if candidate.get("signature") == signature
                        ),
                        None,
                    )
                    if matching_index is not None:
                        operation = queue.pop(matching_index)
                if operation is None:
                    # Older starts did not carry an id while newer adapters
                    # may put it only on the end/result. Match those records
                    # by the legacy name/signature queue, but never attach an
                    # explicitly-correlated end to a different id.
                    candidates = [
                        candidate
                        for candidate in tool_operations
                        if candidate.get("end") is None
                        and candidate.get("tool_name") == tool_name
                        and str(candidate.get("key") or "").startswith("name:")
                        and candidate.get("signature") == signature
                    ]
                    if stable_id and len(candidates) != 1:
                        candidates = []
                    if not candidates:
                        legacy_candidates = [
                            candidate
                            for candidate in tool_operations
                            if candidate.get("end") is None
                            and candidate.get("tool_name") == tool_name
                            and str(candidate.get("key") or "").startswith("name:")
                        ]
                        # A unique legacy start can safely be paired with a
                        # newly-correlated end even when older history did
                        # not retain its arguments. With multiple candidates,
                        # require the signature to avoid cross-pairing.
                        if not stable_id or len(legacy_candidates) == 1:
                            candidates = legacy_candidates
                    if candidates:
                        candidate_key = str(candidates[0].get("key") or "")
                        queue = open_tool_operations.get(candidate_key, [])
                        queue_key = candidate_key
                        try:
                            operation = queue.pop(queue.index(candidates[0]))
                        except (ValueError, IndexError):
                            operation = candidates[0]
                if operation is None:
                    operation = queue.pop(0) if queue else {
                        "start": None,
                        "end": None,
                        "key": queue_key,
                        "tool_name": tool_name,
                        "used": False,
                    }
                if not queue:
                    open_tool_operations.pop(queue_key, None)
                if operation not in tool_operations:
                    tool_operations.append(operation)
                operation["end"] = event
            continue

        if event_type in {"tool.end", "tool.failed"}:
            # AgentRunToolCall が実内容を保持するため、記録ライフサイクルは表示しない。
            continue

        items.append((created_at, int(event.sequence or 0), _timeline_event_item(run, event)))

    for operation in agent_operations:
        start = operation.get("start")
        end = operation.get("end")
        base_event = end or start
        if base_event is None:
            continue
        item = _timeline_event_item(run, base_event)
        start_payload = start.payload if start and isinstance(start.payload, dict) else {}
        end_payload = end.payload if end and isinstance(end.payload, dict) else {}
        started_at = start.created_at if start else None
        ended_at = end.created_at if end else run.ended_at if interrupted_status else None
        duration_ms = None
        if started_at and ended_at and ended_at >= started_at:
            duration_ms = int((ended_at - started_at).total_seconds() * 1000)
        result = _payload_text(end_payload, "result", "result_preview") or None
        error = _payload_text(end_payload, "error") or (
            str(run.error) if interrupted_status == "failed" and run.error else None
        )
        task = _payload_text(start_payload, "task")
        label = str(item.get("actor_label") or "サブエージェント")
        # 子 run のタイムラインへ辿れるよう、集約 item にも子 run id を残す。
        child_run_id = (
            _payload_text(end_payload, "child_run_id")
            or _payload_text(start_payload, "child_run_id")
            or None
        )
        item.update(
            {
                "id": f"operation:agent:{start.id if start else end.id}",
                "event_type": "agent_operation",
                "visibility": "normal",
                "child_run_id": child_run_id,
                "status": (
                    interrupted_status
                    if end is None and interrupted_status
                    else "running"
                    if end is None
                    else "failed"
                    if error or str(end.status) == "failed"
                    else "succeeded"
                ),
                "display_status": (
                    interrupted_status
                    if end is None and interrupted_status
                    else "started"
                    if end is None
                    else "failed"
                    if error or str(end.status) == "failed"
                    else "succeeded"
                ),
                "action": task or label,
                "message": None,
                "result": _clip(result),
                "result_preview": _clip(result, max_chars=1200),
                "error": error,
                "success": (
                    False
                    if end is None and interrupted_status == "failed"
                    else None
                    if end is None
                    else not bool(error or str(end.status) == "failed")
                ),
                "duration_ms": duration_ms,
                "payload": {**_jsonable(start_payload), **_jsonable(end_payload)},
                "created_at": _dt(started_at or ended_at),
                "started_at": _dt(started_at),
                "ended_at": _dt(ended_at),
            }
        )
        items.append((started_at or ended_at or datetime.min, int(start.sequence if start else end.sequence or 0), item))

    for index, tool_call in enumerate(tool_calls):
        tool_name = _normalize_tool_name(_clean_tool_name(tool_call.tool_name))
        correlation_id = str(tool_call.tool_call_id or "")
        call_arguments = tool_call.arguments if isinstance(tool_call.arguments, dict) else {}
        call_signature = (
            _clean_tool_name(tool_call.tool_name)
            if _looks_like_shell_command(_clean_tool_name(tool_call.tool_name))
            else json.dumps(call_arguments, sort_keys=True, ensure_ascii=False)
        )
        candidates = [
            operation
            for operation in tool_operations
            if not operation["used"]
            and operation["tool_name"] == tool_name
            and (
                operation["key"] == f"id:{correlation_id}"
                if correlation_id
                else operation.get("signature") == call_signature
            )
        ]
        if not candidates and not correlation_id:
            candidates = [
                operation
                for operation in tool_operations
                if not operation["used"] and operation["tool_name"] == tool_name
            ]
        if not candidates and correlation_id:
            candidates = [
                operation
                for operation in tool_operations
                if not operation["used"]
                and operation["tool_name"] == tool_name
                and operation["key"].startswith("name:")
            ]
        operation = candidates[0] if candidates else None
        if operation:
            operation["used"] = True
        start = operation.get("start") if operation else None
        end = operation.get("end") if operation else None
        operation_id = (
            f"operation:tool:{start.id if start else end.id}"
            if start or end
            else None
        )
        created_at = (
            (start.created_at if start else None)
            or tool_call.started_at
            or tool_call.created_at
            or datetime.min
        )
        items.append(
            (
                created_at,
                100000 + index,
                _timeline_tool_call_item(
                    run,
                    tool_call,
                    operation_id=operation_id,
                    operation_started_at=start.created_at if start else None,
                    operation_ended_at=end.created_at if end else None,
                    operation_end_payload=(
                        end.payload if end and isinstance(end.payload, dict) else None
                    ),
                    operation_end_status=end.status if end else None,
                ),
            )
        )

    for operation in tool_operations:
        if operation["used"]:
            continue
        start = operation.get("start")
        end = operation.get("end")
        base_event = end or start
        if base_event is None:
            continue
        item = _timeline_event_item(run, base_event)
        start_payload = start.payload if start and isinstance(start.payload, dict) else {}
        end_payload = end.payload if end and isinstance(end.payload, dict) else {}
        started_at = start.created_at if start else None
        ended_at = end.created_at if end else run.ended_at if interrupted_status else None
        duration_ms = None
        if started_at and ended_at and ended_at >= started_at:
            duration_ms = int((ended_at - started_at).total_seconds() * 1000)
        end_success, end_error, result, _end_explicit = _tool_payload_outcome(
            end_payload,
            event_status=end.status if end else None,
            completed=end is not None,
        )
        if end is None:
            status = interrupted_status or "running"
            display_status = interrupted_status or "started"
            error = (
                str(run.error)
                if interrupted_status == "failed" and run.error
                else None
            )
            success = False if interrupted_status == "failed" else None
        else:
            status = "failed" if end_success is False else "succeeded"
            display_status = status
            error = end_error
            success = False if end_success is False else True
        tool_name = str(operation.get("tool_name") or item.get("tool_name") or "")
        item.update(
            {
                "id": f"operation:tool:{start.id if start else end.id}",
                "event_type": "tool_operation",
                "visibility": "normal",
                "status": status,
                "display_status": display_status,
                "action": _tool_operation_label(tool_name, item.get("actor_label")),
                "message": None,
                "arguments": _event_tool_arguments(start_payload) or _event_tool_arguments(end_payload),
                "result": result,
                "result_preview": _clip(result, max_chars=1200),
                "error": error,
                "success": success,
                "duration_ms": duration_ms,
                "payload": {**_jsonable(start_payload), **_jsonable(end_payload)},
                "created_at": _dt(started_at or ended_at),
                "started_at": _dt(started_at),
                "ended_at": _dt(ended_at),
            }
        )
        items.append((started_at or ended_at or datetime.min, int(start.sequence if start else end.sequence or 0), item))

    return [
        item
        for _created_at, _order, item in sorted(
            items,
            key=lambda row: (row[0], row[1]),
        )
    ]


class ExecutedReceiptMatch(str):
    """Verdict: same invocation already executed successfully (see ``result``)."""

    def __new__(cls, result: str = "") -> "ExecutedReceiptMatch":
        instance = super().__new__(cls, "already_executed_same_intent")
        instance.result = result
        return instance


class AgentRunService:
    """Create and update durable agent execution records."""

    def __init__(self, db_manager: Any | None = None, *, config: Any | None = None) -> None:
        self._db_manager = db_manager
        self.config = config

    def _get_db_manager(self) -> Any:
        return self._db_manager or get_database_manager()

    async def _session(self) -> AsyncSession:
        db_manager = self._get_db_manager()
        value = db_manager.get_session()
        return await value if asyncio.iscoroutine(value) or hasattr(value, "__await__") else value

    async def _ensure_interactive_work_in_session(
        self,
        db_session: Any,
        run: AgentRun,
        *,
        conversation: Any | None = None,
        user_id: str | None = None,
        client_message_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        binding: Any | None = None,
    ) -> Any | None:
        """Enlist a Chat AgentRun in the common Work ledger transaction."""

        if str(getattr(run, "run_type", "") or "") != "chat_turn" or run.session_id is None:
            return None
        if conversation is None:
            conversation = await db_session.get(ConversationSession, run.session_id)
        if conversation is None:
            # Legacy/unit fixtures may create a human chat run with only a
            # session UUID.  Typed Agent runs must still fail closed below;
            # without a session row there is no canonical Agent to bind.
            if run.agent_id is not None:
                raise ValueError("Session not found or deleted")
            return None
        if getattr(conversation, "deleted_at", None) is not None:
            raise ValueError("Session not found or deleted")
        if binding is None:
            resolved_agent, resolved_revision, binding = await _pin_session_agent_binding(
                db_session,
                conversation,
                user_id=user_id or getattr(run, "user_id", None),
                requested_agent_id=str(run.agent_id) if run.agent_id else None,
                requested_revision_id=(
                    str(run.agent_revision_id) if run.agent_revision_id else None
                ),
                config=self.config,
            )
        else:
            resolved_agent = parse_uuid(getattr(binding, "agent_id", None))
            resolved_revision = parse_uuid(getattr(binding, "agent_revision_id", None))
            if resolved_agent is None or resolved_revision is None:
                raise ValueError("conversation Agent binding snapshot is incomplete")
        if resolved_agent is None:
            return None
        run.agent_id = resolved_agent
        run.agent_revision_id = resolved_revision
        binding_projection = _binding_audit_projection(binding)
        if binding_projection:
            current_metadata = dict(run.run_metadata or {})
            current_metadata["agent_binding"] = binding_projection
            run.run_metadata = _safe_audit_metadata(
                current_metadata,
                run_type=run.run_type,
            )
            manifest = dict(run.resolved_execution_manifest or {})
            # Keep provider route evidence in the existing bounded manifest;
            # binding ID/hash remain audit metadata so the manifest stays a
            # reproducibility projection rather than a second authority.
            if binding_projection.get("route_snapshot"):
                route_snapshot = binding_projection["route_snapshot"]
                if isinstance(route_snapshot, Mapping) and isinstance(
                    route_snapshot.get("roles"), Mapping
                ):
                    route_snapshot = route_snapshot["roles"]
                manifest["route_snapshot"] = route_snapshot
            manifest.update(
                {
                    "agent_id": str(resolved_agent),
                    "agent_revision_id": str(resolved_revision),
                    "agent_revision_version": binding_projection.get("revision_version"),
                    "source": "conversation_agent",
                }
            )
            bounded_manifest = _bounded_execution_manifest(manifest)
            if bounded_manifest is not None:
                run.resolved_execution_manifest = bounded_manifest
                run.execution_manifest_hash = hashlib.sha256(
                    json.dumps(
                        bounded_manifest,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
        from .agent_work_runtime import AgentWorkCoordinator

        coordinator = AgentWorkCoordinator(
            self._get_db_manager(),
            enabled=True,
            config=self.config,
            worker_id="interactive-chat",
        )
        return await coordinator.admit_interactive(
            session_id=str(run.session_id),
            run_id=str(run.id),
            agent_id=str(resolved_agent),
            agent_revision_id=str(resolved_revision),
            user_id=user_id or getattr(run, "user_id", None),
            project_id=str(run.project_id) if run.project_id else None,
            task_id=str(run.task_id) if run.task_id else None,
            client_message_id=client_message_id or getattr(run, "client_message_id", None),
            source_revision=str(resolved_revision),
            metadata=metadata or getattr(run, "run_metadata", None) or {},
            session=db_session,
        )

    @staticmethod
    def _root_dialogue_route_identity(
        binding: Any | None,
        run: AgentRun,
        *,
        config: Any | None = None,
    ) -> tuple[str | None, str | None]:
        """Read the immutable Root route captured by the Agent binding.

        A worker Dialogue may carry a worker rate card in its authorization
        row.  Root continuation Runs must resolve their own ``operator``
        route from the Root Agent binding instead of inheriting that card.
        The binding snapshot is preferred; the live App Config is only used
        as a compatibility fallback for old snapshots that did not persist
        the role projection.
        """

        provider: str | None = None
        model: str | None = None
        projection = _binding_audit_projection(binding)
        route_snapshot = projection.get("route_snapshot")
        if route_snapshot is None and binding is not None:
            route_snapshot = getattr(binding, "route_snapshot", None)
        if isinstance(route_snapshot, Mapping):
            roles = route_snapshot.get("roles", route_snapshot)
            route = roles.get("operator") if isinstance(roles, Mapping) else None
            if isinstance(route, Mapping) and not route.get("error"):
                provider = str(route.get("provider") or "").strip() or None
                model = str(route.get("model") or "").strip() or None

        if (not provider or not model) and binding is not None:
            routing_pins = getattr(binding, "routing_pins", None)
            if callable(routing_pins):
                try:
                    from .agent_model_routing import (
                        AgentModelRouteError,
                        resolve_agent_role_route,
                    )

                    route = resolve_agent_role_route(
                        config,
                        routing_pins(),
                        "operator",
                    )
                    provider = str(getattr(route, "provider", "") or "").strip() or None
                    model = str(getattr(route, "model", "") or "").strip() or None
                except (AgentModelRouteError, TypeError, ValueError):
                    pass

        # ``provider``/``model`` on a newly-created Run should be empty, but
        # retaining an exact already-resolved pair makes this helper safe for
        # compatibility fixtures and prevents a foreign card from becoming a
        # route authority.
        run_provider = str(getattr(run, "provider", "") or "").strip() or None
        run_model = str(getattr(run, "model", "") or "").strip() or None
        provider = provider or run_provider
        model = model or run_model
        return provider, model

    async def _root_dialogue_server_rate_card(
        self,
        *,
        provider: str | None,
        model: str | None,
        max_cost_micros: int | None,
        currency: str | None,
    ) -> dict[str, Any] | None:
        """Resolve the exact Root provider/model card before a priced call."""

        if max_cost_micros is None:
            return None
        if str(currency or "").strip().upper() != "USD":
            raise AgentChatAdmissionError(
                "configured Dialogue cost budgets require the server USD rate card",
                code="dialogue_cost_rate_card_unavailable",
            )
        normalized_provider = str(provider or "").strip()
        normalized_model = str(model or "").strip()
        if not normalized_provider or not normalized_model:
            raise AgentChatAdmissionError(
                "configured Root cost budget has no exact provider route",
                code="dialogue_cost_rate_card_unavailable",
            )
        try:
            from .pricing import get_pricing_engine

            engine = get_pricing_engine()
            await engine.ensure_loaded()
            rule = engine.resolve(normalized_provider, normalized_model)
        except Exception as exc:
            raise AgentChatAdmissionError(
                "configured Root cost budget has no server rate card",
                code="dialogue_cost_rate_card_unavailable",
            ) from exc
        if rule is None or str(getattr(rule, "pricing_kind", "")) not in {
            "flat_token",
            "tiered_token",
        }:
            raise AgentChatAdmissionError(
                "configured Root cost budget has no server rate card",
                code="dialogue_cost_rate_card_unavailable",
            )

        rate_sets: list[Mapping[str, Any]] = []
        rates = getattr(rule, "rates", None)
        if isinstance(rates, Mapping):
            rate_sets.append(rates)
        for tier in getattr(rule, "tiers", ()) or ():
            if isinstance(tier, Mapping) and isinstance(tier.get("rates"), Mapping):
                rate_sets.append(tier["rates"])

        def highest_per_1k(key: str) -> str | None:
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
            return format(max(values), "f") if values else None

        input_rate = highest_per_1k("input")
        output_rate = highest_per_1k("output")
        if input_rate is None or output_rate is None:
            raise AgentChatAdmissionError(
                "configured Root cost budget has incomplete server rates",
                code="dialogue_cost_rate_card_unavailable",
            )
        card: dict[str, Any] = {
            "provider": normalized_provider[:64],
            "model": normalized_model[:160],
            "input_per_1k": input_rate,
            "output_per_1k": output_rate,
            "currency": "USD",
        }
        cached_rate = highest_per_1k("cached_input")
        if cached_rate is not None:
            card["cached_per_1k"] = cached_rate
        return card

    async def _admit_root_dialogue_budget_in_session(
        self,
        session: Any,
        run: AgentRun,
        *,
        metadata: Mapping[str, Any],
        binding: Any | None,
    ) -> dict[str, Any] | None:
        """Reserve and project one Root Dialogue continuation before dispatch.

        The Root Run is still an ordinary Chat Agent Run.  This narrow hook
        only activates when its server-authored Dialogue marker says
        ``role=root``; legacy Chat runs therefore keep their existing path.
        Financial counters are owned by the chain's ``BudgetRoot`` while the
        current child Dialogue remains an additional local ceiling.
        """

        marker = metadata.get("agent_dialogue") if isinstance(metadata, Mapping) else None
        if not isinstance(marker, Mapping):
            return None
        if str(marker.get("role") or "").strip().casefold() != "root":
            return None
        dialogue_uuid = parse_uuid(marker.get("dialogue_id"))
        if dialogue_uuid is None:
            raise AgentChatAdmissionError(
                "Root Dialogue budget identity is incomplete",
                code="dialogue_root_budget_identity_invalid",
            )
        dialogue = (
            await session.execute(
                select(AgentDialogue)
                .where(AgentDialogue.id == dialogue_uuid)
                .with_for_update()
            )
        ).scalars().first()
        run_id = parse_uuid(run.id)
        if dialogue is None or run_id is None:
            raise AgentChatAdmissionError(
                "Root Dialogue budget owner is unavailable",
                code="dialogue_root_budget_owner_missing",
            )
        if (
            parse_uuid(run.session_id) != parse_uuid(dialogue.root_session_id)
            or parse_uuid(run.agent_id) != parse_uuid(dialogue.root_agent_id)
            or parse_uuid(run.agent_revision_id) != parse_uuid(dialogue.root_agent_revision_id)
            or str(run.user_id or "") != str(dialogue.owner_user_id or "")
        ):
            raise AgentChatAdmissionError(
                "Root Dialogue budget identity conflicts with Run",
                code="dialogue_root_budget_identity_conflict",
            )

        from .agent_dialogue_budget import (
            DialogueBudgetError,
            budget_from_dialogue,
            get_dialogue_budget_owner,
            project_dialogue_budget,
            reserve_dialogue_budget,
        )

        try:
            budget_owner = await get_dialogue_budget_owner(
                session,
                dialogue,
                lock=True,
            )
            owner_budget = budget_from_dialogue(budget_owner)
            local_budget = budget_from_dialogue(dialogue)
            # Root and child turn/attempt fences are both exact.  The helper
            # reserves the shared aggregate; these local checks prevent a
            # child-local zero-capacity turn from slipping through with a
            # zero-token/cost reservation.
            if int(getattr(dialogue, "turn_count", 0) or 0) + 1 > local_budget.max_turns:
                raise DialogueBudgetError(
                    "dialogue turn budget exhausted",
                    code="dialogue_turn_budget_exhausted",
                )
            if int(getattr(dialogue, "attempt_count", 0) or 0) + 1 > local_budget.max_attempts:
                raise DialogueBudgetError(
                    "dialogue attempt budget exhausted",
                    code="dialogue_attempt_budget_exhausted",
                )
            if int(getattr(budget_owner, "turn_count", 0) or 0) + 1 > owner_budget.max_turns:
                raise DialogueBudgetError(
                    "dialogue turn budget exhausted",
                    code="dialogue_turn_budget_exhausted",
                )
            if int(getattr(budget_owner, "attempt_count", 0) or 0) + 1 > owner_budget.max_attempts:
                raise DialogueBudgetError(
                    "dialogue attempt budget exhausted",
                    code="dialogue_attempt_budget_exhausted",
                )

            def remaining_tokens(row: Any, budget: Any) -> int | None:
                if budget.max_tokens is None:
                    return None
                return max(
                    0,
                    int(budget.max_tokens)
                    - int(getattr(row, "total_tokens", 0) or 0)
                    - int(getattr(row, "reserved_tokens", 0) or 0),
                )

            def remaining_cost(row: Any, budget: Any) -> int | None:
                if budget.max_cost_micros is None:
                    return None
                return max(
                    0,
                    int(budget.max_cost_micros)
                    - int(getattr(row, "cost_micros", 0) or 0)
                    - int(getattr(row, "reserved_cost_micros", 0) or 0),
                )

            owner_tokens = remaining_tokens(budget_owner, owner_budget)
            local_tokens = remaining_tokens(dialogue, local_budget)
            token_capacity = (
                min(value for value in (owner_tokens, local_tokens) if value is not None)
                if owner_tokens is not None or local_tokens is not None
                else 0
            )
            owner_cost = remaining_cost(budget_owner, owner_budget)
            local_cost = remaining_cost(dialogue, local_budget)
            cost_capacity = (
                min(value for value in (owner_cost, local_cost) if value is not None)
                if owner_cost is not None or local_cost is not None
                else 0
            )
            if token_capacity <= 0 and (owner_tokens is not None or local_tokens is not None):
                raise DialogueBudgetError(
                    "dialogue token budget exhausted",
                    code="dialogue_token_budget_exhausted",
                )
            if cost_capacity <= 0 and (owner_cost is not None or local_cost is not None):
                raise DialogueBudgetError(
                    "dialogue cost budget exhausted",
                    code="dialogue_cost_budget_exhausted",
                )

            provider, model = self._root_dialogue_route_identity(
                binding,
                run,
                config=self.config,
            )
            if provider and model:
                run.provider = provider
                run.model = model
            effective_cost_ceiling = (
                owner_budget.max_cost_micros
                if owner_budget.max_cost_micros is not None
                else local_budget.max_cost_micros
            )
            effective_currency = owner_budget.cost_currency or local_budget.cost_currency
            server_rate_card = await self._root_dialogue_server_rate_card(
                provider=provider,
                model=model,
                max_cost_micros=effective_cost_ceiling,
                currency=effective_currency,
            )
            reservation_key = f"run:{run_id}"
            await reserve_dialogue_budget(
                session,
                dialogue_uuid,
                turns=1,
                attempts=1,
                tokens=int(token_capacity or 0),
                cost_micros=int(cost_capacity or 0),
                reservation_key=reservation_key,
                run_id=run_id,
            )
            projection = project_dialogue_budget(
                dialogue,
                budget_owner=budget_owner,
            )
            projection.update(
                {
                    "role": "root",
                    "budget_owner_dialogue_id": str(
                        getattr(budget_owner, "root_dialogue_id", None) or dialogue.id
                    ),
                    "local_dialogue_id": str(dialogue.id),
                    "local_remaining_tokens": local_tokens,
                    "local_remaining_cost_micros": local_cost,
                    "provider_max_output_tokens": int(token_capacity or 0) or None,
                    "provider_reserved_cost_micros": int(cost_capacity or 0) or None,
                }
            )
            if server_rate_card is not None:
                projection["server_rate_card"] = server_rate_card
            marker_copy = dict(marker)
            marker_copy["budget"] = projection
            marker_copy["provider"] = provider
            marker_copy["model"] = model
            merged_metadata = dict(run.run_metadata or {})
            merged_metadata["agent_dialogue"] = marker_copy
            run.run_metadata = _safe_audit_metadata(
                merged_metadata,
                run_type=run.run_type,
            )
            return projection
        except DialogueBudgetError as exc:
            raise AgentChatAdmissionError(str(exc), code=exc.code) from exc

    async def _materialize_root_dialogue_budget_blocked_in_session(
        self,
        session: Any,
        run: AgentRun,
        *,
        metadata: Mapping[str, Any],
        binding: Any | None,
        error_code: str,
    ) -> None:
        """Persist a safe Root blocked outcome without starting a provider.

        The source Worker artifact has already been written as the trigger
        message by ``create_or_get_dispatch_turn``.  When Root admission is
        denied by a finite/unknown budget, keep that artifact, terminalize its
        Run/Work, cancel the dispatch outbox, and append one service-authored
        notice.  The provider is never invoked and a retry cannot replay the
        source blindly.
        """

        marker = metadata.get("agent_dialogue") if isinstance(metadata, Mapping) else None
        dialogue_uuid = (
            parse_uuid(marker.get("dialogue_id"))
            if isinstance(marker, Mapping)
            else None
        )
        run_uuid = parse_uuid(run.id)
        if dialogue_uuid is None or run_uuid is None:
            raise AgentChatAdmissionError(
                "Root Dialogue budget identity is incomplete",
                code="dialogue_root_budget_identity_invalid",
            )
        dialogue = (
            await session.execute(
                select(AgentDialogue)
                .where(AgentDialogue.id == dialogue_uuid)
                .with_for_update()
            )
        ).scalars().first()
        if dialogue is None:
            raise AgentChatAdmissionError(
                "Root Dialogue budget owner is unavailable",
                code="dialogue_root_budget_owner_missing",
            )

        # Work admission still goes through the common identity/lease path so
        # the blocked outcome has a canonical Work row and event.  It is
        # intentionally called only after the budget denial and never claims
        # a provider lease.
        work_result = await self._ensure_interactive_work_in_session(
            session,
            run,
            conversation=await session.get(ConversationSession, run.session_id),
            user_id=str(run.user_id or ""),
            client_message_id=str(run.client_message_id or ""),
            metadata=run.run_metadata if isinstance(run.run_metadata, Mapping) else metadata,
            binding=binding,
        )
        work_uuid = parse_uuid(
            work_result.get("id")
            if isinstance(work_result, Mapping)
            else getattr(work_result, "id", None)
        ) or parse_uuid(
            work_result.get("work_item_id") if isinstance(work_result, Mapping) else None
        )
        work = await session.get(AgentWorkItem, work_uuid) if work_uuid else None
        now = datetime.utcnow()
        safe_code = str(error_code or "dialogue_budget_blocked")[:96]
        run_metadata = dict(run.run_metadata or {})
        marker_copy = dict(marker) if isinstance(marker, Mapping) else {}
        marker_copy.update(
            {
                "safe_error_code": safe_code,
                "provider_started": False,
                "budget_blocked": True,
            }
        )
        run_metadata["agent_dialogue"] = marker_copy
        run.run_metadata = _safe_audit_metadata(run_metadata, run_type=run.run_type)
        run.status = "failed"
        run.error = safe_code
        run.result = {
            "safe_error_code": safe_code,
            "outcome_classification": "blocked",
            "provider_started": False,
            "dialogue_id": str(dialogue_uuid),
            "run_id": str(run_uuid),
        }
        run.validation = {}
        run.ended_at = run.ended_at or now
        run.updated_at = now
        run.last_event_at = now
        if work is not None:
            old_state = str(work.state or "")
            if old_state not in {"succeeded", "failed", "cancelled", "dead_letter"}:
                work.state = "blocked"
                work.outcome_classification = "blocked"
                work.safe_error_code = safe_code
                work.blocker_code = safe_code
                work.escalation_reason = "Agent Dialogue budget admission blocked"
                work.lease_owner = None
                work.lease_token = None
                work.lease_expires_at = None
                work.heartbeat_at = now
                work.updated_at = now
                work.completed_at = now
                work.active_agent_run_id = None
                work_sequence = int(
                    await session.scalar(
                        select(func.max(AgentWorkEvent.sequence)).where(
                            AgentWorkEvent.work_item_id == work.id
                        )
                    )
                    or 0
                ) + 1
                session.add(
                    AgentWorkEvent(
                        id=uuid.uuid4(),
                        work_item_id=work.id,
                        sequence=work_sequence,
                        event_type="work.blocked",
                        from_state=old_state,
                        to_state="blocked",
                        actor_kind="service",
                        actor_id="aoitalk.system",
                        actor_service_key="aoitalk.system",
                        agent_run_id=run_uuid,
                        safe_error_code=safe_code,
                        message="Agent Dialogue budget admission blocked",
                        payload_json={
                            "source": "agent_dialogue_budget_admission",
                            "dialogue_id": str(dialogue_uuid),
                            "run_id": str(run_uuid),
                            "provider_started": False,
                        },
                        created_at=now,
                    )
                )

        dialogue.state = "blocked"
        dialogue.updated_at = now
        budget_root_id = parse_uuid(getattr(dialogue, "budget_root_dialogue_id", None))
        if budget_root_id is not None:
            from ..memory.models import AgentDialogueBudgetRoot

            budget_root = await session.get(AgentDialogueBudgetRoot, budget_root_id)
            if budget_root is not None:
                budget_root.state = "blocked"
                budget_root.updated_at = now

        # Deterministic identity makes recovery/re-entry idempotent even when
        # the caller repeats the same client message after seeing the notice.
        notice_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"aoitalk:agent-dialogue-budget-blocked:{dialogue_uuid}:{run_uuid}",
        )
        notice = await session.get(ConversationMessage, notice_id)
        root_session_id = parse_uuid(dialogue.root_session_id)
        if notice is None and root_session_id is not None:
            source_message = await session.get(ConversationMessage, run.trigger_message_id)
            notice = ConversationMessage(
                id=notice_id,
                session_id=root_session_id,
                role="assistant",
                content=(
                    "Agentの予算上限に達したため、この受信内容は保存しました。"
                    "予算を確認してから再開してください。"
                ),
                parent_message_id=(
                    source_message.id
                    if source_message is not None and source_message.session_id == root_session_id
                    else None
                ),
                branch_index=0,
                is_active_branch=True,
                message_metadata={
                    "agent_dialogue_id": str(dialogue_uuid),
                    "root_run_id": str(run_uuid),
                    "safe_error_code": safe_code,
                    "budget_blocked": True,
                    "provider_started": False,
                },
                sender_type="system",
                sender_id="aoitalk.system",
                sender_display_name="AoiTalk System",
                created_at=now,
                updated_at=now,
            )
            session.add(notice)
            await session.execute(
                update(ConversationSession)
                .where(ConversationSession.id == root_session_id)
                .values(
                    message_count=ConversationSession.message_count + 1,
                    last_activity=_monotonic_activity(now),
                    # ``blocked`` is a Work/Dialogue state, not a valid
                    # ConversationSession development status.  Keep the
                    # session projection in the existing UI contract while
                    # the typed Work/Dialogue projection carries the block.
                    development_status="waiting_for_user",
                )
            )

    @staticmethod
    def _dialogue_digest(value: Any) -> str:
        canonical = json.dumps(
            _jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def _dialogue_human_answer_request_fingerprint(
        cls,
        *,
        dialogue_id: Any,
        input_id: Any,
        input_revision: int,
        control_revision: int,
        body_digest: str,
        recovery_from_run_id: Any = None,
    ) -> str:
        """Build the one canonical fingerprint for a persisted Human answer.

        ``None`` is deliberate here.  The normal answer producer and a late
        transport replay must hash the same shape, including the absent
        recovery reference, so an idempotent Root lookup cannot drift into a
        second Run because one caller omitted that field.
        """

        return cls._dialogue_digest(
            {
                "dialogue_id": str(dialogue_id),
                "input_id": str(input_id),
                "input_revision": int(input_revision),
                "control_revision": int(control_revision),
                "body_digest": str(body_digest),
                "recovery_from_run_id": (
                    str(recovery_from_run_id)
                    if recovery_from_run_id not in (None, "")
                    else None
                ),
            }
        )

    @classmethod
    def _dialogue_continuation_target(
        cls,
        *,
        dialogue: Any,
        input_row: Any,
        source_run: Any,
        source_work: Any,
        source_message: Any,
        answer_message_id: Any,
        root_run_id: Any,
        root_work_item_id: Any,
    ) -> dict[str, Any]:
        """Project one server-authored Human-answer execution target."""

        values = {
            "target_kind": "human_answer_continuation",
            "dialogue_id": str(getattr(dialogue, "id", "") or ""),
            "input_id": str(getattr(input_row, "id", "") or ""),
            "input_revision": int(getattr(input_row, "revision", 0) or 0),
            "control_revision": int(
                getattr(dialogue, "control_revision", 0) or 0
            ),
            "source_run_id": str(getattr(source_run, "id", "") or ""),
            "source_work_item_id": str(
                getattr(source_work, "id", "") or ""
            ),
            "source_message_id": str(
                getattr(source_message, "id", "") or ""
            ),
            "source_session_id": str(
                getattr(source_run, "session_id", "") or ""
            ),
            "source_agent_id": str(getattr(source_run, "agent_id", "") or ""),
            "source_agent_revision_id": str(
                getattr(source_run, "agent_revision_id", "") or ""
            ),
            "answer_message_id": str(answer_message_id or ""),
            "root_session_id": str(
                getattr(dialogue, "root_session_id", "") or ""
            ),
            "root_agent_id": str(getattr(dialogue, "root_agent_id", "") or ""),
            "root_agent_revision_id": str(
                getattr(dialogue, "root_agent_revision_id", "") or ""
            ),
            "root_run_id": str(root_run_id or ""),
            "root_work_item_id": str(root_work_item_id or ""),
        }
        if any(
            values[key] in ("", "None")
            for key in (
                "dialogue_id",
                "input_id",
                "source_run_id",
                "source_work_item_id",
                "source_message_id",
                "source_session_id",
                "source_agent_id",
                "source_agent_revision_id",
                "answer_message_id",
                "root_session_id",
                "root_agent_id",
                "root_agent_revision_id",
                "root_run_id",
                "root_work_item_id",
            )
        ):
            raise AgentChatAdmissionError(
                "dialogue continuation target is incomplete",
                code="dialogue_continuation_target_invalid",
            )
        if values["input_revision"] < 1 or values["control_revision"] < 0:
            raise AgentChatAdmissionError(
                "dialogue continuation target revision is invalid",
                code="dialogue_continuation_target_invalid",
            )
        values["target_digest"] = cls._dialogue_digest(values)
        return values

    async def _require_dialogue_human_owner(
        self,
        db_session: Any,
        owner_user_id: Any,
    ) -> uuid.UUID:
        """Resolve the canonical active human owner for a dialogue turn."""

        owner_uuid = parse_uuid(owner_user_id)
        if owner_uuid is None:
            raise AgentChatAdmissionError(
                "dialogue owner is invalid",
                code="dialogue_human_owner_invalid",
            )
        if await _lookup_agent_for_legacy_user(db_session, owner_uuid) is not None:
            raise AgentChatAdmissionError(
                "Agent principals cannot own dialogue turns",
                code="dialogue_human_owner_agent_forbidden",
            )
        owner = await db_session.get(User, owner_uuid)
        if owner is None or not bool(getattr(owner, "is_active", False)):
            raise AgentChatAdmissionError(
                "active human owner is required",
                code="dialogue_human_owner_required",
            )
        return owner_uuid

    async def _validate_dialogue_project_hint(
        self,
        db_session: Any,
        *,
        project_id: uuid.UUID | None,
        owner_user_id: uuid.UUID,
        receiver_agent_id: uuid.UUID,
    ) -> None:
        """Validate a hint without attaching the root session to a Project."""

        if project_id is None:
            return
        from ..memory.models import Project, ProjectMember

        project = await db_session.get(Project, project_id)
        if project is None or getattr(project, "deleted_at", None) is not None:
            raise AgentChatAdmissionError(
                "dialogue project hint is unavailable",
                code="dialogue_project_hint_unavailable",
            )
        member = await db_session.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == owner_user_id,
            )
        )
        owner_id = parse_uuid(getattr(project, "owner_id", None))
        can_read = owner_id == owner_user_id
        if member is not None:
            permissions = member.permissions if isinstance(member.permissions, dict) else {}
            can_read = can_read or bool(permissions.get("read"))
        if not can_read:
            raise AgentChatAdmissionError(
                "dialogue project hint is not readable by the owner",
                code="dialogue_project_hint_forbidden",
            )
        grant = await db_session.scalar(
            select(AgentProjectGrant).where(
                AgentProjectGrant.agent_id == receiver_agent_id,
                AgentProjectGrant.project_id == project_id,
                AgentProjectGrant.state == "active",
            )
        )
        if grant is None or not bool((grant.permissions or {}).get("read")):
            raise AgentChatAdmissionError(
                "receiver Agent lacks Project read authority",
                code="dialogue_receiver_project_forbidden",
            )

    async def _build_dialogue_human_anchor_from_pinned_reference(
        self,
        db_session: Any,
        *,
        dialogue: Any | None,
        source_human: Mapping[str, Any],
        source_metadata: Mapping[str, Any],
        include_body: bool,
        _seen_runs: set[uuid.UUID],
    ) -> dict[str, Any]:
        """Re-read and verify the original Human authority reference.

        Root finite answer/control Runs have a new Human trigger message.  A
        trigger row is therefore not sufficient authority: the metadata must
        point to the Dialogue's original Human Run/Work/message, and every
        identity, body, binding, manifest and capability digest is checked
        against the ORM rows before the original anchor is returned.
        """

        original_run_uuid = parse_uuid(source_human.get("run_id"))
        original_message_uuid = parse_uuid(source_human.get("message_id"))
        original_work_uuid = parse_uuid(source_human.get("work_item_id"))
        owner_uuid = parse_uuid(source_human.get("owner_user_id"))
        if (
            original_run_uuid is None
            or original_message_uuid is None
            or owner_uuid is None
        ):
            raise AgentChatAdmissionError(
                "dialogue Human authority reference is incomplete",
                code="dialogue_source_human_anchor_invalid",
            )
        if dialogue is not None:
            for field_name, actual in (
                ("human_run_id", original_run_uuid),
                ("human_message_id", original_message_uuid),
                ("human_work_item_id", original_work_uuid),
            ):
                pinned = parse_uuid(getattr(dialogue, field_name, None))
                if pinned is not None and pinned != actual:
                    raise AgentChatAdmissionError(
                        "dialogue Human authority reference changed",
                        code="dialogue_source_human_anchor_changed",
                    )
            if parse_uuid(getattr(dialogue, "owner_user_id", None)) != owner_uuid:
                raise AgentChatAdmissionError(
                    "dialogue Human authority owner changed",
                    code="dialogue_source_human_owner_changed",
                )
        original_run = await db_session.get(AgentRun, original_run_uuid)
        original_work = (
            await db_session.get(AgentWorkItem, original_work_uuid)
            if original_work_uuid is not None
            else None
        )
        if original_run is None:
            raise AgentChatAdmissionError(
                "dialogue original Human Run is unavailable",
                code="dialogue_source_human_anchor_missing",
            )
        anchor = await self._build_dialogue_human_anchor(
            db_session,
            source_run=original_run,
            source_work=original_work,
            include_body=include_body,
            dialogue=dialogue,
            _seen_runs=_seen_runs,
        )
        stored_body_digest = str(source_human.get("body_digest") or "").casefold()
        if stored_body_digest != str(anchor.get("body_digest") or "").casefold():
            raise AgentChatAdmissionError(
                "dialogue original Human body digest changed",
                code="dialogue_source_human_anchor_changed",
            )
        stored_authority_digest = str(
            source_metadata.get("source_human_authority_digest")
            or source_metadata.get("authority_digest")
            or ""
        ).casefold()
        if stored_authority_digest != str(anchor.get("authority_digest") or "").casefold():
            raise AgentChatAdmissionError(
                "dialogue original Human authority digest changed",
                code="dialogue_source_human_authority_changed",
            )
        stored_refs = dict(source_human)
        stored_refs.pop("body_digest", None)
        stored_refs.pop("authority_digest", None)
        actual_refs = dict(anchor["refs"])
        if self._dialogue_digest(stored_refs) != self._dialogue_digest(actual_refs):
            raise AgentChatAdmissionError(
                "dialogue original Human authority reference changed",
                code="dialogue_source_human_authority_changed",
            )
        return anchor

    async def _build_dialogue_human_anchor(
        self,
        db_session: Any,
        *,
        source_run: AgentRun,
        source_work: Any | None,
        include_body: bool = False,
        dialogue: Any | None = None,
        _seen_runs: set[uuid.UUID] | None = None,
    ) -> dict[str, Any]:
        """Read and pin the canonical Human source for one root AgentRun.

        Agent instructions never serve as an authority fallback.  The source
        must be the Run's trigger user message, with the Run owner, Work link,
        binding snapshot and normalized command capabilities projected into a
        digest-only anchor.  Body text is returned only to the request-local
        terminal caller and is never written to AgentDialogue metadata.
        """

        seen_runs = set(_seen_runs or ())
        source_run_id = parse_uuid(getattr(source_run, "id", None))
        if source_run_id is not None:
            if source_run_id in seen_runs:
                raise AgentChatAdmissionError(
                    "dialogue Human anchor provenance cycles",
                    code="dialogue_source_human_anchor_invalid",
                )
            seen_runs.add(source_run_id)
        owner_uuid = parse_uuid(getattr(source_run, "user_id", None))
        message_uuid = parse_uuid(getattr(source_run, "trigger_message_id", None))
        session_uuid = parse_uuid(getattr(source_run, "session_id", None))
        if owner_uuid is None or message_uuid is None or session_uuid is None:
            raise AgentChatAdmissionError(
                "canonical Human source anchor is incomplete",
                code="dialogue_source_human_anchor_invalid",
            )
        await self._require_dialogue_human_owner(db_session, owner_uuid)
        message = await db_session.get(ConversationMessage, message_uuid)
        canonical_human_message = (
            message is not None
            and message.session_id == session_uuid
            and str(message.role or "").casefold() == "user"
            and str(message.sender_type or "").casefold() in {"user", "human"}
            and parse_uuid(message.sender_id) == owner_uuid
        )
        run_metadata = source_run.run_metadata if isinstance(source_run.run_metadata, dict) else {}
        dialogue_metadata = run_metadata.get("agent_dialogue")
        source_human = (
            dialogue_metadata.get("source_human")
            if isinstance(dialogue_metadata, Mapping)
            else None
        )
        if isinstance(dialogue_metadata, Mapping) and isinstance(source_human, Mapping):
            # This covers Worker deliveries and Root finite Human
            # answer/control Runs.  The persisted source_human reference is
            # accepted only after a same-DB re-read of its original rows.
            return await self._build_dialogue_human_anchor_from_pinned_reference(
                db_session,
                dialogue=dialogue,
                source_human=source_human,
                source_metadata=dialogue_metadata,
                include_body=include_body,
                _seen_runs=seen_runs,
            )
        if not canonical_human_message:
            # A receipt-triggered Agent message is evidence of an earlier
            # admitted Delivery.  Follow its Dialogue/original Human Run
            # anchor; never reinterpret the Agent message as a new Human
            # authority source.
            original_run_id = parse_uuid(
                source_human.get("run_id")
                if isinstance(source_human, Mapping)
                else None
            )
            if dialogue is not None:
                original_run_id = original_run_id or parse_uuid(
                    getattr(dialogue, "human_run_id", None)
                )
            if original_run_id is not None and original_run_id not in seen_runs:
                original_run = await db_session.get(AgentRun, original_run_id)
                if original_run is not None:
                    original_work_id = parse_uuid(
                        source_human.get("work_item_id")
                        if isinstance(source_human, Mapping)
                        else None
                    )
                    if dialogue is not None:
                        original_work_id = original_work_id or parse_uuid(
                            getattr(dialogue, "human_work_item_id", None)
                        )
                    original_work = (
                        await db_session.get(AgentWorkItem, original_work_id)
                        if original_work_id is not None
                        else None
                    )
                    return await self._build_dialogue_human_anchor(
                        db_session,
                        source_run=original_run,
                        source_work=original_work,
                        include_body=include_body,
                        dialogue=dialogue,
                        _seen_runs=seen_runs,
                    )
            raise AgentChatAdmissionError(
                "source Run trigger is not a canonical Human message",
                code="dialogue_source_human_anchor_invalid",
            )
        if (
            message is None
            or message.session_id != session_uuid
            or str(message.role or "").casefold() != "user"
            or str(message.sender_type or "").casefold() not in {"user", "human"}
            or parse_uuid(message.sender_id) != owner_uuid
        ):
            raise AgentChatAdmissionError(
                "source Run trigger is not a canonical Human message",
                code="dialogue_source_human_anchor_invalid",
            )
        metadata = source_run.run_metadata if isinstance(source_run.run_metadata, dict) else {}
        binding_metadata = metadata.get("agent_binding") if isinstance(metadata, dict) else None
        if not isinstance(binding_metadata, Mapping):
            binding_metadata = {}
        message_metadata = message.message_metadata if isinstance(message.message_metadata, dict) else {}
        raw_caps = message_metadata.get("command_capabilities")
        if not isinstance(raw_caps, (list, tuple, set, frozenset)):
            raw_caps = []
        command_caps = sorted(
            {
                str(item).strip().casefold()
                for item in raw_caps
                if str(item).strip()
            }
        )[:64]
        refs = {
            "message_id": str(message_uuid),
            "run_id": str(source_run.id),
            "work_item_id": str(source_work.id) if source_work is not None else None,
            "owner_user_id": str(owner_uuid),
            "session_id": str(session_uuid),
            "binding_hash": str(binding_metadata.get("snapshot_hash") or "")[:64] or None,
            "manifest_hash": str(source_run.execution_manifest_hash or "")[:64] or None,
            "command_capabilities": command_caps,
        }
        authority_digest = self._dialogue_digest(refs)
        body = str(message.content or "")
        result = {
            "refs": refs,
            "authority_digest": authority_digest,
            "body_digest": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        }
        if include_body:
            result["body"] = body
        return result

    async def load_dialogue_human_anchor(
        self,
        *,
        run_id: str,
        dialogue_id: str | None = None,
    ) -> dict[str, Any]:
        """Load the request-local Human body from pinned Worker metadata."""

        run_uuid = parse_uuid(run_id)
        dialogue_uuid = parse_uuid(dialogue_id)
        if run_uuid is None:
            raise AgentChatAdmissionError(
                "invalid dialogue Worker Run",
                code="dialogue_worker_run_invalid",
            )
        db_session = await self._session()
        try:
            run = await db_session.get(AgentRun, run_uuid)
            if run is None:
                raise AgentChatAdmissionError(
                    "dialogue Worker Run not found",
                    code="dialogue_worker_run_not_found",
                )
            metadata = run.run_metadata if isinstance(run.run_metadata, dict) else {}
            dialogue_metadata = metadata.get("agent_dialogue")
            if not isinstance(dialogue_metadata, Mapping):
                raise AgentChatAdmissionError(
                    "dialogue Worker provenance is missing",
                    code="dialogue_worker_provenance_missing",
                )
            stored_id = parse_uuid(dialogue_metadata.get("dialogue_id"))
            if dialogue_uuid is not None and stored_id != dialogue_uuid:
                raise AgentChatAdmissionError(
                    "dialogue Worker provenance mismatch",
                    code="dialogue_worker_provenance_mismatch",
                )
            anchor = dialogue_metadata.get("source_human")
            if not isinstance(anchor, Mapping):
                raise AgentChatAdmissionError(
                    "dialogue Human anchor is missing",
                    code="dialogue_source_human_anchor_missing",
                )

            async def _read_anchor(
                candidate: Mapping[str, Any],
                *,
                expected_authority_digest: str | None = None,
            ) -> dict[str, Any]:
                message_uuid = parse_uuid(candidate.get("message_id"))
                owner_uuid = parse_uuid(candidate.get("owner_user_id"))
                if message_uuid is None or owner_uuid is None:
                    raise AgentChatAdmissionError(
                        "dialogue Human anchor is invalid",
                        code="dialogue_source_human_anchor_invalid",
                    )
                await self._require_dialogue_human_owner(db_session, owner_uuid)
                message = await db_session.get(ConversationMessage, message_uuid)
                if (
                    message is None
                    or parse_uuid(message.session_id)
                    != parse_uuid(candidate.get("session_id"))
                    or str(message.role or "").casefold() != "user"
                    or str(message.sender_type or "").casefold()
                    not in {"user", "human"}
                    or parse_uuid(message.sender_id) != owner_uuid
                ):
                    raise AgentChatAdmissionError(
                        "dialogue Human anchor message is invalid",
                        code="dialogue_source_human_anchor_invalid",
                    )
                body = str(message.content or "")
                body_digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
                if str(candidate.get("body_digest") or "").casefold() != body_digest:
                    raise AgentChatAdmissionError(
                        "dialogue Human anchor body changed",
                        code="dialogue_source_human_anchor_changed",
                    )
                refs = dict(candidate)
                candidate_authority_digest = str(
                    refs.pop("authority_digest", "") or ""
                ).casefold()
                authority_refs = dict(refs)
                authority_refs.pop("body_digest", None)
                expected_digest = self._dialogue_digest(authority_refs)
                stored_authority_digest = str(
                    expected_authority_digest or candidate_authority_digest
                ).casefold()
                if stored_authority_digest != expected_digest:
                    raise AgentChatAdmissionError(
                        "dialogue Human authority digest changed",
                        code="dialogue_source_human_authority_changed",
                    )
                return {
                    "body": body,
                    "refs": refs,
                    "authority_digest": stored_authority_digest,
                }

            original_anchor = await _read_anchor(
                anchor,
                expected_authority_digest=(
                    str(
                        dialogue_metadata.get("source_human_authority_digest")
                        or dialogue_metadata.get("authority_digest")
                        or ""
                    ).casefold()
                ),
            )
            answer_anchor = dialogue_metadata.get("source_human_answer")
            if not isinstance(answer_anchor, Mapping):
                answer_anchor = dialogue_metadata.get("source_human_control")
            if isinstance(answer_anchor, Mapping):
                answer = await _read_anchor(answer_anchor)
                for key in (
                    "owner_user_id",
                    "run_id",
                    "work_item_id",
                    "binding_hash",
                    "manifest_hash",
                    "command_capabilities",
                ):
                    if answer["refs"].get(key) != original_anchor["refs"].get(key):
                        raise AgentChatAdmissionError(
                            "dialogue Human answer authority scope changed",
                            code="dialogue_source_human_answer_scope_changed",
                        )
                # A Root continuation runs in the session that holds the saved
                # Human answer.  A Worker continuation runs in its own session
                # and receives that answer relayed by the Root, so the answer
                # must live in the delivery's source (Root) session instead.
                expected_answer_session = parse_uuid(run.session_id)
                if str(dialogue_metadata.get("role") or "").casefold() == "worker":
                    expected_answer_session = parse_uuid(
                        dialogue_metadata.get("source_session_id")
                    )
                answer_session = parse_uuid(answer["refs"].get("session_id"))
                if answer_session is None or answer_session != expected_answer_session:
                    raise AgentChatAdmissionError(
                        "dialogue Human answer session changed",
                        code="dialogue_source_human_answer_session_changed",
                    )
                return {
                    # ``body``/``refs`` remain the original Human authority
                    # anchor used by ToolPolicy.  The current finite answer
                    # is retained separately and is never presented as the
                    # original request body or permission source.
                    "body": original_anchor["body"],
                    "refs": original_anchor["refs"],
                    "authority_digest": original_anchor["authority_digest"],
                    "current_body": answer["body"],
                    "current_refs": answer["refs"],
                    "current_authority_digest": answer["authority_digest"],
                }
            return original_anchor
        finally:
            await db_session.close()

    async def _next_agent_dialogue_delivery_sequence(
        self,
        db_session: Any,
        dialogue_id: uuid.UUID,
    ) -> int:
        """Allocate one Dialogue delivery sequence under its row lock."""

        # Every delivery lane (request, response, control, and receipt) must
        # serialize on the same Dialogue row.  Flush before reading the max so
        # autoflush-disabled sessions include deliveries already staged in this
        # transaction; otherwise a control delivery can reuse a pending
        # request sequence and trip the per-Dialogue unique constraint.
        await db_session.execute(
            select(AgentDialogue.id)
            .where(AgentDialogue.id == dialogue_id)
            .with_for_update()
        )
        await db_session.flush()
        current = await db_session.scalar(
            select(func.max(AgentDialogueDelivery.sequence)).where(
                AgentDialogueDelivery.dialogue_id == dialogue_id
            )
        )
        return 0 if current is None else int(current) + 1

    async def dispatch_trusted_agent_dialogue(
        self,
        *,
        dialogue_id: str | None = None,
        content: str | None = None,
        source_agent_id: str | None = None,
        source_agent_revision_id: str | None = None,
        source_session_id: str | None = None,
        source_run_id: str | None = None,
        receiver_session_id: str | None = None,
        receiver_agent_id: str | None = None,
        receiver_agent_revision_id: str | None = None,
        idempotency_key: str | None = None,
        source_work_item_id: str | None = None,
        source_message_id: str | None = None,
        receiver_work_item_id: str | None = None,
        context_project_hint_id: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        control_revision: int | None = None,
        request_digest: str | None = None,
        outbox_payload: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        depth: int = 0,
        session: Any | None = None,
        dialogue: Any | None = None,
        delivery: Any | None = None,
        source_message: Any | None = None,
        provenance_hash: str | None = None,
    ) -> Dict[str, Any]:
        """Atomically deliver one trusted Agent→Agent dialogue turn.

        The source identity is resolved from the canonical source Run and its
        session, never from sender display fields.  The receiver gets a new
        ordinary Chat Run/Work pair; cross-Agent causality is represented by a
        validated ``AgentRunEdge`` and never by ``parent_run_id``.
        """

        if dialogue is not None or delivery is not None or source_message is not None:
            if dialogue is None or delivery is None or source_message is None or session is None:
                raise AgentChatAdmissionError(
                    "dialogue handoff requires one locked DB session",
                    code="dialogue_handoff_session_required",
                )
            return await self._handoff_dialogue_delivery(
                session=session,
                dialogue=dialogue,
                delivery=delivery,
                source_message=source_message,
                provenance_hash=provenance_hash or "",
            )

        dialogue_uuid = parse_uuid(dialogue_id)
        source_agent_uuid = parse_uuid(source_agent_id)
        source_revision_uuid = parse_uuid(source_agent_revision_id)
        source_session_uuid = parse_uuid(source_session_id)
        source_run_uuid = parse_uuid(source_run_id)
        receiver_session_uuid = parse_uuid(receiver_session_id)
        receiver_agent_uuid = parse_uuid(receiver_agent_id)
        receiver_revision_uuid = parse_uuid(receiver_agent_revision_id)
        source_work_uuid = parse_uuid(source_work_item_id)
        source_message_uuid = parse_uuid(source_message_id)
        requested_project_uuid = parse_uuid(
            project_id or context_project_hint_id
        )
        task_uuid = parse_uuid(task_id)
        normalized_key = str(idempotency_key or "").strip()
        if not normalized_key or len(normalized_key) > 255:
            raise ValueError("valid dialogue idempotency_key is required")
        if any(
            value is None
            for value in (
                dialogue_uuid,
                source_agent_uuid,
                source_revision_uuid,
                source_session_uuid,
                source_run_uuid,
                receiver_session_uuid,
                receiver_agent_uuid,
                receiver_revision_uuid,
            )
        ):
            raise ValueError("dialogue dispatch identity is invalid")
        if project_id and context_project_hint_id and project_id != context_project_hint_id:
            raise AgentChatAdmissionError(
                "dialogue project hint conflicts with explicit project",
                code="dialogue_project_scope_conflict",
            )
        try:
            depth_value = max(0, min(int(depth), 32))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid dialogue depth") from exc

        db_session = await self._session()
        try:
            dialogue = (
                await db_session.execute(
                    select(AgentDialogue)
                    .where(AgentDialogue.id == dialogue_uuid)
                    .with_for_update()
                )
            ).scalars().first()
            if dialogue is None:
                raise AgentChatAdmissionError(
                    "dialogue not found",
                    code="dialogue_not_found",
                )
            if dialogue.root_session_id != source_session_uuid:
                raise AgentChatAdmissionError(
                    "source session is not the dialogue root",
                    code="dialogue_source_session_mismatch",
                )
            if dialogue.root_agent_id != source_agent_uuid:
                raise AgentChatAdmissionError(
                    "source Agent binding mismatch",
                    code="dialogue_source_agent_mismatch",
                )
            if dialogue.worker_session_id != receiver_session_uuid or dialogue.worker_agent_id != receiver_agent_uuid or dialogue.worker_agent_revision_id != receiver_revision_uuid:
                raise AgentChatAdmissionError(
                    "receiver Agent binding mismatch",
                    code="dialogue_receiver_binding_mismatch",
                )
            expected_control = int(dialogue.control_revision or 0)
            if control_revision is not None and int(control_revision) != expected_control:
                raise DispatchConflictError(
                    "dialogue control revision changed",
                    code="dialogue_control_revision_changed",
                )
            if str(dialogue.state or "") in {"cancelled", "failed", "expired", "succeeded"}:
                raise AgentChatAdmissionError(
                    "dialogue is terminal",
                    code="dialogue_terminal",
                )

            source_agent = await db_session.get(Agent, source_agent_uuid)
            receiver_agent = await db_session.get(Agent, receiver_agent_uuid)
            if source_agent is None or str(source_agent.state or "") != "active":
                raise AgentChatAdmissionError("source Agent is inactive", code="dialogue_source_agent_inactive")
            if receiver_agent is None or str(receiver_agent.state or "") != "active":
                raise AgentChatAdmissionError("receiver Agent is inactive", code="dialogue_receiver_agent_inactive")
            source_revision = await db_session.get(AgentRevision, source_revision_uuid)
            receiver_revision = await db_session.get(AgentRevision, receiver_revision_uuid)
            anchor_revision = await db_session.get(
                AgentRevision,
                dialogue.root_agent_revision_id,
            )
            if source_revision is None or source_revision.agent_id != source_agent_uuid:
                raise AgentChatAdmissionError("source revision mismatch", code="dialogue_source_revision_mismatch")
            if (
                anchor_revision is None
                or anchor_revision.agent_id != source_agent_uuid
                or int(source_revision.version or 0) < int(anchor_revision.version or 0)
            ):
                raise AgentChatAdmissionError(
                    "source AgentRevision is not a current continuation",
                    code="dialogue_source_revision_mismatch",
                )
            if receiver_revision is None or receiver_revision.agent_id != receiver_agent_uuid:
                raise AgentChatAdmissionError("receiver revision mismatch", code="dialogue_receiver_revision_mismatch")

            source_run = (
                await db_session.execute(
                    select(AgentRun)
                    .where(AgentRun.id == source_run_uuid)
                    .with_for_update()
                )
            ).scalars().first()
            if source_run is None or source_run.session_id != source_session_uuid:
                raise AgentChatAdmissionError("source Run mismatch", code="dialogue_source_run_mismatch")
            if source_run.agent_id != source_agent_uuid or source_run.agent_revision_id != source_revision_uuid:
                raise AgentChatAdmissionError("source Run Agent mismatch", code="dialogue_source_run_agent_mismatch")
            if source_run.parent_run_id is not None:
                raise AgentChatAdmissionError("source Run must be root-scoped", code="dialogue_source_run_parent_forbidden")
            if str(source_run.status or "").casefold() in RUN_TERMINAL_STATUSES:
                raise AgentChatAdmissionError(
                    "terminal source Run cannot mint a new dialogue command",
                    code="dialogue_source_run_terminal",
                )
            owner_uuid = await self._require_dialogue_human_owner(db_session, source_run.user_id)
            if parse_uuid(dialogue.owner_user_id) != owner_uuid:
                raise AgentChatAdmissionError("dialogue human owner mismatch", code="dialogue_owner_mismatch")
            if source_work_uuid is not None and source_run.work_item_id != source_work_uuid:
                raise AgentChatAdmissionError("source Work mismatch", code="dialogue_source_work_mismatch")
            source_work = None
            if source_work_uuid is not None:
                source_work = await db_session.get(AgentWorkItem, source_work_uuid)
                if source_work is None or source_work.assigned_agent_id != source_agent_uuid:
                    raise AgentChatAdmissionError("source Work Agent mismatch", code="dialogue_source_work_agent_mismatch")
            if source_message_uuid is not None:
                source_message = await db_session.get(ConversationMessage, source_message_uuid)
                if source_message is None or source_message.session_id != source_session_uuid:
                    raise AgentChatAdmissionError("source message mismatch", code="dialogue_source_message_mismatch")
                sender_id = parse_uuid(getattr(source_message, "sender_id", None))
                if sender_id != source_agent_uuid or str(getattr(source_message, "sender_type", "") or "") != "agent":
                    raise AgentChatAdmissionError("source message sender mismatch", code="dialogue_source_sender_mismatch")
            source_human_anchor = await self._build_dialogue_human_anchor(
                db_session,
                source_run=source_run,
                source_work=source_work,
                include_body=False,
                dialogue=dialogue,
            )

            root_session = await db_session.get(ConversationSession, source_session_uuid)
            receiver_session = (
                await db_session.execute(
                    select(ConversationSession)
                    .where(
                        ConversationSession.id == receiver_session_uuid,
                        ConversationSession.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
            ).scalars().first()
            if root_session is None or root_session.deleted_at is not None:
                raise AgentChatAdmissionError("dialogue root session is unavailable", code="dialogue_root_session_unavailable")
            if receiver_session is None or receiver_session.agent_id != receiver_agent_uuid:
                raise AgentChatAdmissionError("receiver session binding mismatch", code="dialogue_receiver_session_mismatch")
            if str(receiver_session.user_id or "") != str(owner_uuid):
                raise AgentChatAdmissionError("receiver session owner mismatch", code="dialogue_receiver_owner_mismatch")
            # AoiDots' root is deliberately projectless.  A validated hint is
            # stored on the dialogue/work projection only.
            if root_session.project_id is not None:
                raise AgentChatAdmissionError("dialogue root session must be projectless", code="dialogue_root_project_forbidden")
            await self._validate_dialogue_project_hint(
                db_session,
                project_id=requested_project_uuid,
                owner_user_id=owner_uuid,
                receiver_agent_id=receiver_agent_uuid,
            )

            existing = (
                await db_session.execute(
                    select(AgentDialogueDelivery)
                    .where(
                        AgentDialogueDelivery.dialogue_id == dialogue_uuid,
                        AgentDialogueDelivery.idempotency_key == normalized_key,
                    )
                    .with_for_update()
                )
            ).scalars().first()
            digest_payload = {
                "dialogue_id": str(dialogue_uuid),
                "content": str(content or ""),
                "source_agent_id": str(source_agent_uuid),
                "source_agent_revision_id": str(source_revision_uuid),
                "source_run_id": str(source_run_uuid),
                "receiver_agent_id": str(receiver_agent_uuid),
                "receiver_agent_revision_id": str(receiver_revision_uuid),
                "receiver_session_id": str(receiver_session_uuid),
                "project_id": str(requested_project_uuid) if requested_project_uuid else None,
                "task_id": str(task_uuid) if task_uuid else None,
                "control_revision": expected_control,
            }
            computed_digest = self._dialogue_digest(digest_payload)
            supplied_digest = str(request_digest or computed_digest).strip().lower()
            if supplied_digest != computed_digest:
                raise DispatchConflictError(
                    "dialogue request digest mismatch",
                    code="dialogue_request_digest_mismatch",
                )
            if existing is not None:
                if existing.request_digest != computed_digest:
                    raise DispatchConflictError(
                        "dialogue idempotency key was reused with different content",
                        code="dialogue_idempotency_conflict",
                    )
                await db_session.commit()
                return existing.to_safe_dict()

            delivery_id = uuid.uuid4()
            message_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"aoitalk:agent-dialogue-message:{dialogue_uuid}:{normalized_key}",
            )
            run_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"aoitalk:agent-dialogue-run:{dialogue_uuid}:{normalized_key}",
            )
            now = datetime.utcnow()
            safe_content = str(content or "").strip()
            if not safe_content:
                raise ValueError("dialogue content is required")
            source_name = str(getattr(source_agent, "display_name", "Agent") or "Agent")[:160]
            message_metadata = {
                "agent_dialogue_id": str(dialogue_uuid),
                "agent_dialogue_delivery_id": str(delivery_id),
                "source_agent_id": str(source_agent_uuid),
                "source_agent_revision_id": str(source_revision_uuid),
                "source_run_id": str(source_run_uuid),
                "control_revision": expected_control,
                "context_project_hint_id": str(requested_project_uuid) if requested_project_uuid else None,
            }
            message = ConversationMessage(
                id=message_id,
                session_id=receiver_session_uuid,
                role="user",
                content=safe_content,
                client_message_id=normalized_key[:512],
                sender_type="agent",
                sender_id=str(source_agent_uuid),
                sender_display_name=source_name,
                message_metadata=_jsonable(message_metadata),
                created_at=now,
                updated_at=now,
            )
            db_session.add(message)
            await db_session.flush()
            run_metadata = dict(metadata or {})
            run_metadata["agent_dialogue"] = {
                "dialogue_id": str(dialogue_uuid),
                "delivery_id": str(delivery_id),
                "role": "worker",
                "source_agent_id": str(source_agent_uuid),
                "source_agent_revision_id": str(source_revision_uuid),
                "source_run_id": str(source_run_uuid),
                "control_revision": expected_control,
                "context_project_hint_id": str(requested_project_uuid) if requested_project_uuid else None,
                "source_human": {
                    **dict(source_human_anchor["refs"]),
                    "body_digest": source_human_anchor["body_digest"],
                },
                "authority_digest": source_human_anchor["authority_digest"],
            }
            authorization = (
                dialogue.authorization_json
                if isinstance(getattr(dialogue, "authorization_json", None), Mapping)
                else {}
            )
            causal_chain = (
                authorization.get("causal_chain")
                if isinstance(authorization.get("causal_chain"), Mapping)
                else None
            )
            if causal_chain is not None:
                run_metadata["agent_dialogue"]["causal_chain"] = {
                    key: causal_chain[key]
                    for key in (
                        "chain_id",
                        "depth",
                        "max_depth",
                        "budget_owner_dialogue_id",
                        "parent_dialogue_id",
                        "parent_delivery_id",
                    )
                    if causal_chain.get(key) not in (None, "")
                }
                run_metadata["agent_dialogue"]["causal_depth"] = int(
                    causal_chain.get("depth") or 0
                )
                run_metadata["agent_dialogue"]["budget_root_dialogue_id"] = str(
                    causal_chain.get("budget_owner_dialogue_id")
                    or getattr(dialogue, "budget_root_dialogue_id", "")
                )
                run_metadata["agent_dialogue"]["parent_dialogue_id"] = str(
                    causal_chain.get("parent_dialogue_id")
                    or getattr(dialogue, "parent_dialogue_id", "")
                )
            run = AgentRun(
                id=run_id,
                root_run_id=run_id,
                parent_run_id=None,
                session_id=receiver_session_uuid,
                trigger_message_id=message_id,
                project_id=requested_project_uuid,
                task_id=task_uuid,
                agent_id=receiver_agent_uuid,
                agent_revision_id=receiver_revision_uuid,
                user_id=str(owner_uuid),
                client_message_id=normalized_key[:512],
                client_message_key=dispatch_client_message_key(normalized_key),
                request_fingerprint=computed_digest,
                run_type="chat_turn",
                status="queued",
                title="",
                objective=safe_content,
                result={},
                validation={},
                run_metadata=_safe_audit_metadata(run_metadata, run_type="chat_turn"),
                created_at=now,
                updated_at=now,
                last_event_at=now,
            )
            db_session.add(run)
            await db_session.flush()
            # Reserve one bounded dialogue turn/attempt only after the
            # idempotency fence has proved this delivery is new and the child
            # Run exists for the optional reservation FK.  The reservation,
            # Run/Work, and Delivery rows share this transaction, so a failed
            # insert rolls every counter back and a replay cannot charge the
            # same request twice.
            try:
                from .agent_dialogue_budget import (
                    DialogueBudgetError,
                    assert_dialogue_budget_available,
                    budget_from_dialogue,
                    get_dialogue_budget_owner,
                    project_dialogue_budget,
                    reserve_dialogue_budget,
                )
                from ..memory.models import AgentDialogueReservation

                # The child Dialogue owns provider/session identity, while
                # the server-derived root aggregate owns chain consumption.
                # Lock/read that owner before calculating admission so a
                # successor cannot reset the root turn/attempt/tokens.
                budget_owner = await get_dialogue_budget_owner(
                    db_session,
                    dialogue,
                    lock=True,
                )
                dialogue_budget = budget_from_dialogue(budget_owner)
                reserved_tokens = max(
                    0,
                    int(dialogue_budget.max_tokens or 0)
                    - int(getattr(budget_owner, "total_tokens", 0) or 0)
                    - int(getattr(budget_owner, "reserved_tokens", 0) or 0),
                )
                reserved_cost_micros = max(
                    0,
                    int(dialogue_budget.max_cost_micros or 0)
                    - int(getattr(budget_owner, "cost_micros", 0) or 0)
                    - int(getattr(budget_owner, "reserved_cost_micros", 0) or 0),
                )
                # A child may narrow the root allowance, but never enlarge
                # it.  The child identity remains the provider-facing scope;
                # root aggregate counters remain the shared billing owner.
                child_budget = budget_from_dialogue(dialogue)
                if child_budget.max_tokens is not None:
                    child_remaining_tokens = max(
                        0,
                        int(child_budget.max_tokens)
                        - int(getattr(dialogue, "total_tokens", 0) or 0)
                        - int(getattr(dialogue, "reserved_tokens", 0) or 0),
                    )
                    reserved_tokens = min(reserved_tokens, child_remaining_tokens)
                if child_budget.max_cost_micros is not None:
                    child_remaining_cost = max(
                        0,
                        int(child_budget.max_cost_micros)
                        - int(getattr(dialogue, "cost_micros", 0) or 0)
                        - int(getattr(dialogue, "reserved_cost_micros", 0) or 0),
                    )
                    reserved_cost_micros = min(
                        reserved_cost_micros,
                        child_remaining_cost,
                    )
                reservation_key = f"run:{run_id}"
                budget_root_id = getattr(budget_owner, "root_dialogue_id", None)
                reservation_scope = (
                    AgentDialogueReservation.budget_root_dialogue_id == budget_root_id
                    if budget_root_id is not None
                    else AgentDialogueReservation.dialogue_id == dialogue_uuid
                )
                existing_reservation = (
                    await db_session.execute(
                        select(AgentDialogueReservation).where(
                            reservation_scope,
                            AgentDialogueReservation.reservation_key == reservation_key,
                        ).with_for_update()
                    )
                ).scalars().first()
                if existing_reservation is None:
                    budget_row = await reserve_dialogue_budget(
                        db_session,
                        dialogue_uuid,
                        turns=1,
                        attempts=1,
                        tokens=reserved_tokens,
                        cost_micros=reserved_cost_micros,
                        reservation_key=reservation_key,
                        run_id=run_id,
                    )
                else:
                    if (
                        str(existing_reservation.state or "") != "reserved"
                        or not parse_uuid(existing_reservation.run_id) == run_id
                    ):
                        raise DialogueBudgetError(
                            "reservation state conflicts with child Run",
                            code="reservation_conflict",
                        )
                    if (
                        int(existing_reservation.reserved_tokens or 0) == reserved_tokens
                        and int(existing_reservation.reserved_cost_micros or 0)
                        == reserved_cost_micros
                    ):
                        # The Dialogue service may have already reserved the
                        # shared chain turn/attempt.  Re-admission is
                        # idempotent and must not increment them again.
                        budget_row = dialogue
                    elif (
                        int(existing_reservation.reserved_tokens or 0) == 0
                        and int(existing_reservation.reserved_cost_micros or 0) == 0
                    ):
                        # Top up the same server-derived reservation with the
                        # provider capacity while preserving its pre-reserved
                        # turn/attempt counters.
                        assert_dialogue_budget_available(
                            budget_owner,
                            turns=0,
                            attempts=0,
                            tokens=reserved_tokens,
                            cost_micros=reserved_cost_micros,
                        )
                        budget_owner.reserved_tokens = int(
                            budget_owner.reserved_tokens or 0
                        ) + reserved_tokens
                        budget_owner.reserved_cost_micros = int(
                            budget_owner.reserved_cost_micros or 0
                        ) + reserved_cost_micros
                        # ``reserve_dialogue_budget`` mirrors the initial
                        # turn/attempt reservation into a distinct child
                        # scope.  This branch only tops up provider capacity
                        # on an already-reserved key, so mirror tokens/cost
                        # into that same child row without touching
                        # turn_count/attempt_count a second time.  A root
                        # Dialogue shares the owner row and must not be
                        # incremented twice.
                        if (
                            parse_uuid(getattr(budget_owner, "root_dialogue_id", None))
                            is not None
                            and parse_uuid(getattr(dialogue, "id", None))
                            != parse_uuid(getattr(budget_owner, "root_dialogue_id", None))
                        ):
                            dialogue.reserved_tokens = int(
                                getattr(dialogue, "reserved_tokens", 0) or 0
                            ) + reserved_tokens
                            dialogue.reserved_cost_micros = int(
                                getattr(dialogue, "reserved_cost_micros", 0) or 0
                            ) + reserved_cost_micros
                        existing_reservation.reserved_tokens = reserved_tokens
                        existing_reservation.reserved_cost_micros = reserved_cost_micros
                        await db_session.flush()
                        budget_row = dialogue
                    else:
                        raise DialogueBudgetError(
                            "reservation payload changed",
                            code="reservation_conflict",
                        )
                budget_owner = await get_dialogue_budget_owner(
                    db_session,
                    dialogue,
                    lock=True,
                )
                budget_projection = project_dialogue_budget(
                    dialogue,
                    budget_owner=budget_owner,
                )
                budget_projection["provider_max_output_tokens"] = (
                    reserved_tokens or None
                )
                budget_projection["provider_reserved_cost_micros"] = (
                    reserved_cost_micros or None
                )
                authorization_budget = (
                    dialogue.authorization_json.get("budget")
                    if isinstance(dialogue.authorization_json, Mapping)
                    and isinstance(dialogue.authorization_json.get("budget"), Mapping)
                    else {}
                )
                server_rate_card = authorization_budget.get("server_rate_card")
                if isinstance(server_rate_card, Mapping):
                    safe_rate_card = {}
                    for key in (
                        "provider",
                        "model",
                        "input_per_1k",
                        "output_per_1k",
                        "cached_per_1k",
                        "currency",
                    ):
                        value = server_rate_card.get(key)
                        if value not in (None, ""):
                            safe_rate_card[key] = str(value)[:160]
                    if "input_per_1k" in safe_rate_card and "output_per_1k" in safe_rate_card:
                        budget_projection["server_rate_card"] = safe_rate_card
                # A deployment may pin a smaller provider tool-loop budget
                # for AgentDialogue turns.  Persist only the server config
                # projection; client metadata can never raise this ceiling.
                configured_tool_rounds = None
                if self.config is not None and callable(
                    getattr(self.config, "get", None)
                ):
                    configured_tool_rounds = self.config.get(
                        "runtime.agent_dialogue_max_tool_rounds"
                    )
                try:
                    configured_tool_rounds = (
                        max(1, min(int(configured_tool_rounds), 120))
                        if configured_tool_rounds is not None
                        else None
                    )
                except (TypeError, ValueError, OverflowError):
                    configured_tool_rounds = None
                if configured_tool_rounds is not None:
                    budget_projection["provider_max_tool_rounds"] = (
                        configured_tool_rounds
                    )
                dialogue_marker = dict(run_metadata["agent_dialogue"])
                dialogue_marker["budget"] = budget_projection
                run_metadata["agent_dialogue"] = dialogue_marker
                run.run_metadata = _safe_audit_metadata(
                    run_metadata,
                    run_type="chat_turn",
                )
            except DialogueBudgetError as exc:
                raise AgentChatAdmissionError(
                    str(exc),
                    code=exc.code,
                ) from exc
            work = await self._ensure_interactive_work_in_session(
                db_session,
                run,
                conversation=receiver_session,
                user_id=str(owner_uuid),
                client_message_id=normalized_key,
                metadata=run_metadata,
            )
            work_id = parse_uuid(
                work.get("id") if isinstance(work, Mapping) else getattr(work, "id", None)
            ) or parse_uuid(
                work.get("work_item_id") if isinstance(work, Mapping) else None
            )
            if work_id is None:
                raise RuntimeError("dialogue receiver Work admission returned no WorkItem")
            delivery_sequence = await self._next_agent_dialogue_delivery_sequence(
                db_session,
                dialogue_uuid,
            )
            delivery = AgentDialogueDelivery(
                id=delivery_id,
                dialogue_id=dialogue_uuid,
                source_session_id=source_session_uuid,
                source_agent_id=source_agent_uuid,
                source_agent_revision_id=source_revision_uuid,
                source_run_id=source_run_uuid,
                source_work_item_id=source_work_uuid,
                source_message_id=source_message_uuid,
                receiver_session_id=receiver_session_uuid,
                receiver_agent_id=receiver_agent_uuid,
                receiver_agent_revision_id=receiver_revision_uuid,
                receiver_run_id=run_id,
                receiver_work_item_id=work_id,
                transport_work_item_id=work_id,
                kind="request",
                state="pending",
                sequence=delivery_sequence,
                causation_id=str(source_run_uuid),
                depth=depth_value,
                expected_control_revision=expected_control,
                idempotency_key=normalized_key,
                request_digest=computed_digest,
                created_at=now,
                updated_at=now,
            )
            db_session.add(delivery)
            dialogue.state = "running"
            dialogue.updated_at = now
            outbox_data = {
                **dict(outbox_payload or {}),
                "agent_dialogue_id": str(dialogue_uuid),
                "agent_dialogue_delivery_id": str(delivery_id),
                "source_agent_id": str(source_agent_uuid),
                "source_run_id": str(source_run_uuid),
                "receiver_agent_id": str(receiver_agent_uuid),
                "receiver_run_id": str(run_id),
                "receiver_work_item_id": str(work_id),
                "context_project_hint_id": str(requested_project_uuid) if requested_project_uuid else None,
                "skip_user_persistence": True,
                "persisted_user_message_id": str(message_id),
            }
            db_session.add(
                ConversationDispatchOutbox(
                    run_id=run_id,
                    session_id=receiver_session_uuid,
                    user_id=str(owner_uuid),
                    client_message_id=normalized_key[:512],
                    client_message_key=dispatch_client_message_key(normalized_key),
                    request_fingerprint=computed_digest,
                    payload=_jsonable(outbox_data),
                    status="pending",
                    attempts=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            db_session.add(
                AgentRunEvent(
                    run_id=run_id,
                    sequence=1,
                    event_type="run.queued",
                    status="queued",
                    message="Agent dialogue receiver run queued",
                    payload={
                        "agent_dialogue_id": str(dialogue_uuid),
                        "delivery_id": str(delivery_id),
                        "source_run_id": str(source_run_uuid),
                        "control_revision": expected_control,
                    },
                    created_at=now,
                )
            )
            edge = AgentRunEdge(
                parent_run_id=source_run_uuid,
                child_run_id=run_id,
                purpose="agent_dialogue",
                status="open",
                edge_metadata=_safe_audit_metadata(
                    {
                        "dialogue_id": str(dialogue_uuid),
                        "delivery_id": str(delivery_id),
                        "source_agent_id": str(source_agent_uuid),
                        "receiver_agent_id": str(receiver_agent_uuid),
                        "control_revision": expected_control,
                    }
                ),
                created_at=now,
            )
            db_session.add(edge)
            await db_session.commit()
            await db_session.refresh(delivery)
            result = delivery.to_safe_dict()
            result.update(
                {
                    "receiver_run_id": str(run_id),
                    "receiver_work_item_id": str(work_id),
                    "receiver_message_id": str(message_id),
                    "dialogue_id": str(dialogue_uuid),
                    "control_revision": expected_control,
                    "owner_user_id": str(owner_uuid),
                }
            )
            return result
        except IntegrityError:
            await db_session.rollback()
            existing = (
                await db_session.execute(
                    select(AgentDialogueDelivery).where(
                        AgentDialogueDelivery.dialogue_id == dialogue_uuid,
                        AgentDialogueDelivery.idempotency_key == normalized_key,
                    )
                )
            ).scalars().first()
            if existing is not None and existing.request_digest == (request_digest or existing.request_digest):
                return existing.to_safe_dict()
            raise
        except Exception:
            await db_session.rollback()
            raise
        finally:
            await db_session.close()

    # Transport/service worker handoff remains an internal branch of the
    # explicit ``dispatch_trusted_agent_dialogue`` API.
    async def _handoff_dialogue_delivery(
        self,
        *,
        session: Any,
        dialogue: Any,
        delivery: Any,
        source_message: Any,
        provenance_hash: str,
    ) -> Dict[str, Any]:
        """Materialize an already-admitted delivery in the caller TX.

        ``AgentDialogueTransport`` owns delivery admission/lease and passes
        its locked session here.  This method only creates the ordinary child
        Chat Message/Run/Work/outbox and validated RunEdge; it never mints a
        new ``AgentDialogueDelivery`` and never rejects a terminal source Run
        whose delivery was admitted earlier.
        """

        def value(row: Any, name: str, default: Any = None) -> Any:
            return row.get(name, default) if isinstance(row, Mapping) else getattr(row, name, default)

        dialogue_id = parse_uuid(value(dialogue, "id"))
        delivery_id = parse_uuid(value(delivery, "id"))
        source_session_id = parse_uuid(value(delivery, "source_session_id"))
        source_agent_id = parse_uuid(value(delivery, "source_agent_id"))
        source_revision_id = parse_uuid(value(delivery, "source_agent_revision_id"))
        source_run_id = parse_uuid(value(delivery, "source_run_id"))
        source_work_id = parse_uuid(value(delivery, "source_work_item_id"))
        source_message_id = parse_uuid(value(delivery, "source_message_id"))
        receiver_session_id = parse_uuid(value(delivery, "receiver_session_id"))
        receiver_agent_id = parse_uuid(value(delivery, "receiver_agent_id"))
        receiver_revision_id = parse_uuid(value(delivery, "receiver_agent_revision_id"))
        if any(
            item is None
            for item in (
                dialogue_id,
                delivery_id,
                source_session_id,
                source_agent_id,
                source_revision_id,
                source_run_id,
                source_message_id,
                receiver_session_id,
                receiver_agent_id,
                receiver_revision_id,
            )
        ):
            raise AgentChatAdmissionError(
                "dialogue delivery provenance is incomplete",
                code="dialogue_delivery_provenance_incomplete",
            )
        content = getattr(source_message, "content", None)
        if content in (None, ""):
            content = getattr(source_message, "_content", None)
        if not str(content or "").strip():
            raise AgentChatAdmissionError(
                "dialogue source message body is unavailable",
                code="dialogue_source_message_body_unavailable",
            )
        refs = {
            "dialogue_id": str(dialogue_id),
            "delivery_id": str(delivery_id),
            "source_session_id": str(source_session_id),
            "source_agent_id": str(source_agent_id),
            "source_agent_revision_id": str(source_revision_id),
            "source_run_id": str(source_run_id),
            "source_work_item_id": str(source_work_id) if source_work_id else None,
            "source_message_id": str(source_message_id),
            "receiver_session_id": str(receiver_session_id),
            "receiver_agent_id": str(receiver_agent_id),
            "receiver_agent_revision_id": str(receiver_revision_id),
            "delivery_kind": str(value(delivery, "kind") or ""),
            "expected_control_revision": int(value(delivery, "expected_control_revision", 0) or 0),
            "provenance_hash": str(provenance_hash or ""),
        }
        source_run = await session.get(AgentRun, source_run_id)
        source_work = await session.get(AgentWorkItem, source_work_id) if source_work_id else None
        if source_run is None:
            raise AgentChatAdmissionError(
                "dialogue source Run is unavailable",
                code="dialogue_source_run_mismatch",
            )
        dialogue_root_session_id = parse_uuid(value(dialogue, "root_session_id"))
        dialogue_root_agent_id = parse_uuid(value(dialogue, "root_agent_id"))
        dialogue_worker_session_id = parse_uuid(value(dialogue, "worker_session_id"))
        dialogue_worker_agent_id = parse_uuid(value(dialogue, "worker_agent_id"))
        if (
            receiver_session_id == dialogue_worker_session_id
            and receiver_agent_id == dialogue_worker_agent_id
        ):
            receiver_role = "worker"
        elif (
            receiver_session_id == dialogue_root_session_id
            and receiver_agent_id == dialogue_root_agent_id
        ):
            receiver_role = "root"
        else:
            raise AgentChatAdmissionError(
                "dialogue delivery receiver direction is invalid",
                code="dialogue_delivery_receiver_invalid",
            )
        refs["source_role"] = (
            "root"
            if source_session_id == dialogue_root_session_id
            and source_agent_id == dialogue_root_agent_id
            else "worker"
            if source_session_id == dialogue_worker_session_id
            and source_agent_id == dialogue_worker_agent_id
            else None
        )
        if refs["source_role"] is None:
            raise AgentChatAdmissionError(
                "dialogue delivery source direction is invalid",
                code="dialogue_delivery_source_invalid",
            )
        refs["receiver_role"] = receiver_role
        # Causal chain metadata is copied only from the locked Dialogue's
        # server authorization.  It is routing evidence for the new child;
        # it never replaces the child Run/Work Agent/session/revision checks.
        authorization = (
            dialogue.authorization_json
            if isinstance(getattr(dialogue, "authorization_json", None), Mapping)
            else {}
        )
        causal_chain = (
            authorization.get("causal_chain")
            if isinstance(authorization.get("causal_chain"), Mapping)
            else None
        )
        if causal_chain is not None:
            refs["causal_chain"] = {
                key: causal_chain[key]
                for key in (
                    "chain_id",
                    "depth",
                    "max_depth",
                    "budget_owner_dialogue_id",
                    "parent_dialogue_id",
                    "parent_delivery_id",
                )
                if causal_chain.get(key) not in (None, "")
            }
            refs["causal_depth"] = int(causal_chain.get("depth") or 0)
            refs["budget_root_dialogue_id"] = str(
                causal_chain.get("budget_owner_dialogue_id")
                or getattr(dialogue, "budget_root_dialogue_id", "")
            )
            refs["parent_dialogue_id"] = str(
                causal_chain.get("parent_dialogue_id")
                or getattr(dialogue, "parent_dialogue_id", "")
            )
        # Worker sessions may be project-bound, but the private AoiDots Root
        # session is permanently projectless. Carry the validated project as
        # a read-only hint for Root receipt recovery instead of copying it
        # into the Root Run/Work scope.
        dialogue_context_hint = authorization.get("context_project_hint_id")
        receiver_project_id = (
            parse_uuid(getattr(dialogue, "project_id", None))
            if receiver_role == "worker"
            else None
        )
        receiver_context_project_hint_id = (
            parse_uuid(dialogue_context_hint)
            if receiver_role == "root" and dialogue_context_hint not in (None, "")
            else None
        )
        if receiver_role == "root" and dialogue_context_hint not in (None, ""):
            if receiver_context_project_hint_id is None:
                raise AgentChatAdmissionError(
                    "dialogue Root project hint is invalid",
                    code="dialogue_project_hint_invalid",
                )
            refs["context_project_hint_id"] = str(receiver_context_project_hint_id)
        source_human_anchor = await self._build_dialogue_human_anchor(
            session,
            source_run=source_run,
            source_work=source_work,
            include_body=False,
            dialogue=dialogue,
        )
        refs["source_human"] = {
            **dict(source_human_anchor["refs"]),
            "body_digest": source_human_anchor["body_digest"],
        }
        source_run_dialogue_metadata = (
            getattr(source_run, "run_metadata", {}).get("agent_dialogue")
            if isinstance(getattr(source_run, "run_metadata", None), Mapping)
            else None
        )
        if isinstance(source_run_dialogue_metadata, Mapping):
            for key in (
                "input_id",
                "input_revision",
                "source_human_message_id",
                "source_human_answered",
                "source_human_answer",
                "recovery_from_run_id",
                "resume_answer",
                "resume_input_control_revision",
            ):
                if key in source_run_dialogue_metadata:
                    refs[key] = source_run_dialogue_metadata[key]
        current_refs = source_human_anchor.get("current_refs")
        current_body = source_human_anchor.get("current_body")
        if isinstance(current_refs, Mapping) and current_body is not None:
            refs["source_human_answer"] = {
                **dict(current_refs),
                "body_digest": hashlib.sha256(
                    str(current_body).encode("utf-8")
                ).hexdigest(),
            }
        refs["authority_digest"] = source_human_anchor["authority_digest"]
        refs["role"] = receiver_role
        # A Human answer already creates the canonical finite Root Run before
        # the worker's observational event is handed back to the root.  The
        # event must converge on that exact outbox identity; using the event
        # Delivery id as a fresh Chat client key would create a second Root
        # Run, whose Work then (correctly) collides with the still-pending
        # answer Work on the same conversation.
        continuation_client_message_id: str | None = None
        continuation_request_fingerprint = str(
            value(delivery, "request_digest") or ""
        )
        if receiver_role == "root":
            input_uuid = (
                parse_uuid(value(delivery, "causation_id"))
                if str(value(delivery, "kind") or "").casefold() == "event"
                else None
            )
            try:
                input_row = (
                    await session.get(AgentDialogueInput, input_uuid)
                    if input_uuid is not None
                    else None
                )
            except OperationalError as exc:
                # Rolling fixtures may predate the optional Input ledger. A
                # missing table removes the answer-reuse proof, so continue
                # through the ordinary delivery identity rather than making
                # the whole root handoff unavailable.
                if "agent_dialogue_inputs" not in str(exc).casefold():
                    raise
                input_row = None
            answer_uuid = parse_uuid(
                getattr(input_row, "answer_message_id", None)
                if input_row is not None
                else None
            )
            answer_message = (
                await session.get(ConversationMessage, answer_uuid)
                if answer_uuid is not None
                else None
            )
            if str(value(delivery, "kind") or "").casefold() == "event":
                causation = value(delivery, "causation_id")
                if causation in (None, ""):
                    raise AgentChatAdmissionError(
                        "dialogue event causation is missing",
                        code="dialogue_event_causation_missing",
                    )
                if causation not in (None, "") and input_uuid is None:
                    raise AgentChatAdmissionError(
                        "dialogue event causation identity is invalid",
                        code="dialogue_event_causation_invalid",
                    )
                # ``human_question`` is an explicit escalation target.  Its
                # worker event is a durable notification only: it must wait
                # for the owner's answer and may not mint a Root Run/Work
                # from the worker's question body.  Validate the complete
                # Input scope before deferring so a foreign/mutated Input
                # cannot turn a provenance failure into a harmless retry.
                if input_row is not None and str(input_row.state or "").casefold() == "pending":
                    input_scope = (
                        input_row.source_run_id,
                        input_row.source_work_item_id,
                        input_row.source_message_id,
                    )
                    if (
                        input_row.dialogue_id != dialogue_id
                        or any(item is None for item in input_scope)
                        or input_scope != (
                            source_run_id,
                            source_work_id,
                            source_message_id,
                        )
                        or int(input_row.expected_control_revision or 0)
                        != int(value(delivery, "expected_control_revision", 0) or 0)
                    ):
                        raise AgentChatAdmissionError(
                            "dialogue pending Input provenance does not match Delivery",
                            code="dialogue_pending_input_provenance_mismatch",
                        )
                    if str(input_row.input_kind or "").casefold() == "human_question":
                        from .agent_work_runtime import InteractiveAdmissionError

                        raise InteractiveAdmissionError("dialogue_human_question_pending")
                if input_uuid is not None and input_row is None:
                    previous_delivery = await session.get(
                        AgentDialogueDelivery, input_uuid
                    )
                    source_metadata = (
                        source_message.message_metadata
                        if isinstance(source_message.message_metadata, Mapping)
                        else {}
                    )
                    question_marker = any(
                        source_metadata.get(key) not in (None, "")
                        for key in (
                            "agent_dialogue_input_id",
                            "input_id",
                            "input_type",
                            "dialogue_input_pending",
                        )
                    )
                    source_run_matches = (
                        source_run is not None
                        and str(source_run.run_type or "") == "chat_turn"
                        and source_run.session_id == dialogue.worker_session_id
                        and source_run.agent_id == dialogue.worker_agent_id
                        and source_run.agent_revision_id
                        == dialogue.worker_agent_revision_id
                        and source_run.work_item_id == source_work_id
                        and source_run.trigger_message_id is not None
                    )
                    source_work_matches = (
                        source_work is not None
                        and source_work.id == source_work_id
                        and source_work.conversation_session_id
                        == dialogue.worker_session_id
                        and source_work.assigned_agent_id
                        == dialogue.worker_agent_id
                        and source_work.agent_revision_id
                        == dialogue.worker_agent_revision_id
                        and str(source_work.source_type or "") == "chat_turn"
                        and str(source_work.execution_adapter or "")
                        == "interactive_chat"
                        and source_work.active_agent_run_id
                        in (None, source_run_id)
                    )
                    source_message_matches = (
                        source_message.session_id == dialogue.worker_session_id
                        and str(source_message.role or "").casefold()
                        == "assistant"
                        and str(source_message.sender_type or "").casefold()
                        == "agent"
                        and parse_uuid(source_message.sender_id)
                        == parse_uuid(dialogue.worker_agent_id)
                        and str(source_metadata.get("agent_dialogue_id"))
                        == str(dialogue_id)
                        and str(source_metadata.get("agent_run_id"))
                        == str(source_run_id)
                    )
                    previous_kind = (
                        str(previous_delivery.kind or "").casefold()
                        if previous_delivery is not None
                        else ""
                    )
                    previous_direction_matches = (
                        previous_kind in {"request", "control"}
                        and previous_delivery.source_session_id
                        == dialogue.root_session_id
                        and previous_delivery.source_agent_id
                        == dialogue.root_agent_id
                        and previous_delivery.source_agent_revision_id
                        == dialogue.root_agent_revision_id
                        and previous_delivery.receiver_session_id
                        == dialogue.worker_session_id
                        and previous_delivery.receiver_agent_id
                        == dialogue.worker_agent_id
                        and previous_delivery.receiver_agent_revision_id
                        == dialogue.worker_agent_revision_id
                        and previous_delivery.receiver_run_id == source_run_id
                        and previous_delivery.receiver_work_item_id == source_work_id
                        and previous_delivery.receiver_message_id
                        == source_run.trigger_message_id
                    ) or (
                        previous_kind == "response"
                        and previous_delivery.source_session_id
                        == dialogue.worker_session_id
                        and previous_delivery.source_agent_id
                        == dialogue.worker_agent_id
                        and previous_delivery.source_agent_revision_id
                        == dialogue.worker_agent_revision_id
                        and previous_delivery.source_run_id == source_run_id
                        and previous_delivery.source_work_item_id == source_work_id
                        and previous_delivery.receiver_session_id
                        == dialogue.root_session_id
                        and previous_delivery.receiver_agent_id
                        == dialogue.root_agent_id
                        and previous_delivery.receiver_agent_revision_id
                        == dialogue.root_agent_revision_id
                    )
                    previous_matches = (
                        previous_delivery is not None
                        and previous_delivery.dialogue_id == dialogue_id
                        and previous_kind in {"request", "control", "response"}
                        and str(previous_delivery.state or "").casefold()
                        in {"delivered", "acked", "failed"}
                        and source_run_matches
                        and source_work_matches
                        and source_message_matches
                        and previous_direction_matches
                        and int(previous_delivery.expected_control_revision or 0)
                        == int(value(delivery, "expected_control_revision", 0) or 0)
                        and not question_marker
                    )
                    if not previous_matches:
                        raise AgentChatAdmissionError(
                            "dialogue event predecessor provenance is invalid",
                            code="dialogue_event_predecessor_provenance_invalid",
                        )
                elif input_uuid is not None:
                    input_scope = (
                        input_row.source_run_id,
                        input_row.source_work_item_id,
                        input_row.source_message_id,
                    )
                    if any(value is None for value in input_scope):
                        raise AgentChatAdmissionError(
                            "dialogue answer Input provenance is incomplete",
                            code="dialogue_answer_root_provenance_incomplete",
                        )
                    if input_scope != (
                        source_run_id,
                        source_work_id,
                        source_message_id,
                    ):
                        raise AgentChatAdmissionError(
                            "dialogue answer Input provenance does not match Delivery",
                            code="dialogue_answer_root_provenance_mismatch",
                        )
            input_matches = (
                input_row is not None
                and input_row.dialogue_id == dialogue_id
                and str(getattr(input_row, "state", "")) == "answered"
                and input_row.source_run_id == source_run_id
                and input_row.source_work_item_id == source_work_id
                and input_row.source_message_id == source_message_id
                and int(input_row.expected_control_revision or 0)
                == int(value(delivery, "expected_control_revision", 0) or 0)
            )
            answer_metadata = (
                answer_message.message_metadata
                if answer_message is not None
                and isinstance(answer_message.message_metadata, Mapping)
                else {}
            )
            answer_matches = (
                answer_message is not None
                and answer_message.session_id == receiver_session_id
                and str(answer_message.role or "").casefold() == "user"
                and str(answer_message.sender_type or "").casefold()
                in {"user", "human"}
                and parse_uuid(answer_message.sender_id)
                == parse_uuid(dialogue.owner_user_id)
                and answer_metadata.get("agent_dialogue_id")
                in (None, "", str(dialogue_id))
                and answer_metadata.get("agent_dialogue_input_id")
                in (None, "", str(input_uuid))
            )
            if input_matches and answer_matches and answer_uuid is not None:
                answer_body = str(answer_message.content or "")
                answer_body_digest = hashlib.sha256(
                    answer_body.encode("utf-8")
                ).hexdigest()
                continuation_client_message_id = (
                    f"dialogue-human-answer:{input_uuid}:"
                    f"{int(input_row.revision or 0)}:{answer_uuid}"
                )
                continuation_request_fingerprint = (
                    self._dialogue_human_answer_request_fingerprint(
                        dialogue_id=dialogue_id,
                        input_id=input_uuid,
                        input_revision=int(input_row.revision or 0),
                        control_revision=int(
                            value(delivery, "expected_control_revision", 0) or 0
                        ),
                        body_digest=answer_body_digest,
                        recovery_from_run_id=refs.get("recovery_from_run_id"),
                    )
                )
                refs["input_id"] = str(input_uuid)
                refs["input_revision"] = int(input_row.revision or 0)
                refs["answer_message_id"] = str(answer_uuid)
                # ``prepare_agent_dialogue_input_answer`` and
                # ``dispatch_dialogue_human_input`` are separate HTTP
                # transactions.  During that short window the Input and
                # answer message are valid, but the canonical Human Root Run
                # does not exist yet.  Refuse to mint a worker-question Root
                # Run with Agent content; the transport caller will defer and
                # retry after the Human dispatch commits the real outbox.
                canonical_key = dispatch_client_message_key(
                    continuation_client_message_id
                )
                existing_root = (
                    await session.execute(
                        select(AgentRun)
                        .where(
                            AgentRun.session_id == receiver_session_id,
                            AgentRun.user_id == str(dialogue.owner_user_id),
                            AgentRun.client_message_key == canonical_key,
                        )
                        .with_for_update()
                    )
                ).scalars().first()
                if existing_root is None:
                    from .agent_work_runtime import InteractiveAdmissionError

                    raise InteractiveAdmissionError("interactive_session_busy")
                existing_marker = (
                    existing_root.run_metadata.get("agent_dialogue")
                    if isinstance(existing_root.run_metadata, Mapping)
                    else None
                )
                existing_answer = (
                    existing_marker.get("source_human_answer")
                    if isinstance(existing_marker, Mapping)
                    else None
                )
                existing_outbox = await session.get(
                    ConversationDispatchOutbox,
                    existing_root.id,
                )
                outbox_payload = (
                    existing_outbox.payload
                    if existing_outbox is not None
                    and isinstance(existing_outbox.payload, Mapping)
                    else {}
                )
                outbox_payload_is_mapping = (
                    existing_outbox is not None
                    and isinstance(existing_outbox.payload, Mapping)
                )
                outbox_marker = outbox_payload.get("agent_dialogue")
                root_provenance_matches = (
                    existing_root.trigger_message_id == answer_uuid
                    and getattr(existing_root, "root_run_id", existing_root.id)
                    == existing_root.id
                    and getattr(existing_root, "parent_run_id", None) is None
                    and existing_root.session_id == receiver_session_id
                    and str(getattr(existing_root, "user_id", dialogue.owner_user_id) or "")
                    == str(dialogue.owner_user_id)
                    and existing_root.agent_id == receiver_agent_id
                    and existing_root.agent_revision_id == receiver_revision_id
                    and str(existing_root.run_type or "") == "chat_turn"
                    and str(getattr(existing_root, "client_message_id", "") or "")
                    in {"", continuation_client_message_id}
                    and str(getattr(existing_root, "client_message_key", "") or "")
                    in {"", canonical_key}
                    and str(getattr(existing_root, "request_fingerprint", "") or "")
                    in {"", continuation_request_fingerprint}
                    and isinstance(existing_marker, Mapping)
                    and str(existing_marker.get("dialogue_id")) == str(dialogue_id)
                    and str(existing_marker.get("input_id")) == str(input_uuid)
                    and str(existing_marker.get("source_human_message_id"))
                    == str(answer_uuid)
                    and existing_marker.get("input_revision")
                    == int(input_row.revision or 0)
                    and existing_marker.get("control_revision")
                    == int(value(delivery, "expected_control_revision", 0) or 0)
                    and bool(existing_marker.get("source_human_answered"))
                    and isinstance(existing_answer, Mapping)
                    and str(existing_answer.get("message_id")) == str(answer_uuid)
                )
                root_active_identity_matches = (
                    root_provenance_matches
                    and str(existing_root.status or "")
                    in {"queued", "running", "awaiting_approval", "uncertain"}
                    and existing_outbox is not None
                    and str(existing_outbox.status or "")
                    in {"pending", "claimed", "delivered"}
                    and str(existing_outbox.client_message_key or "") == canonical_key
                    and bool(outbox_payload.get("_dialogue_human_input"))
                    and str(outbox_payload.get("persisted_user_message_id"))
                    == str(answer_uuid)
                    and isinstance(outbox_marker, Mapping)
                    and str(outbox_marker.get("source_human_message_id"))
                    == str(answer_uuid)
                )
                terminal_answer_refs = {
                    key: existing_answer.get(key)
                    for key in (
                        "message_id",
                        "run_id",
                        "work_item_id",
                        "owner_user_id",
                        "session_id",
                        "binding_hash",
                        "manifest_hash",
                        "command_capabilities",
                    )
                } if isinstance(existing_answer, Mapping) else {}
                terminal_root_work = (
                    await session.get(
                        AgentWorkItem,
                        getattr(existing_root, "work_item_id", None),
                    )
                    if getattr(existing_root, "work_item_id", None) is not None
                    else None
                )
                terminal_work_identity_matches = (
                    terminal_root_work is not None
                    and terminal_root_work.id == getattr(existing_root, "work_item_id", None)
                    and str(terminal_root_work.state or "") == "succeeded"
                    and str(terminal_root_work.outcome_classification or "")
                    == "succeeded"
                    and str(terminal_root_work.source_type or "") == "chat_turn"
                    and str(terminal_root_work.execution_adapter or "")
                    == "interactive_chat"
                    and terminal_root_work.conversation_session_id
                    == receiver_session_id
                    and terminal_root_work.assigned_agent_id == receiver_agent_id
                    and terminal_root_work.agent_revision_id == receiver_revision_id
                    and str(terminal_root_work.source_id or "")
                    == str(existing_root.id)
                    and terminal_root_work.active_agent_run_id is None
                    and terminal_root_work.lease_owner is None
                    and terminal_root_work.lease_token is None
                    and terminal_root_work.lease_expires_at is None
                )
                active_root_work_identity_matches = (
                    terminal_root_work is not None
                    and terminal_root_work.id == getattr(existing_root, "work_item_id", None)
                    and str(terminal_root_work.state or "") in {"claimed", "running"}
                    and str(terminal_root_work.source_type or "") == "chat_turn"
                    and str(terminal_root_work.execution_adapter or "")
                    == "interactive_chat"
                    and terminal_root_work.conversation_session_id
                    == receiver_session_id
                    and terminal_root_work.assigned_agent_id == receiver_agent_id
                    and terminal_root_work.agent_revision_id == receiver_revision_id
                    and str(terminal_root_work.source_id or "")
                    == str(existing_root.id)
                    and terminal_root_work.active_agent_run_id == existing_root.id
                    and bool(str(terminal_root_work.lease_owner or "").strip())
                    and bool(str(terminal_root_work.lease_token or "").strip())
                    and terminal_root_work.lease_expires_at is not None
                    and terminal_root_work.lease_expires_at > datetime.utcnow()
                )
                terminal_root_fields_matches = (
                    getattr(existing_root, "root_run_id", None) == existing_root.id
                    and getattr(existing_root, "parent_run_id", None) is None
                    and str(getattr(existing_root, "user_id", "") or "")
                    == str(dialogue.owner_user_id)
                    and str(getattr(existing_root, "client_message_id", "") or "")
                    == continuation_client_message_id
                    and str(getattr(existing_root, "client_message_key", "") or "")
                    == canonical_key
                    and str(getattr(existing_root, "request_fingerprint", "") or "")
                    == continuation_request_fingerprint
                    and getattr(existing_root, "work_item_id", None) is not None
                )
                outbox_identity_matches = (
                    existing_outbox is not None
                    and existing_outbox.run_id == existing_root.id
                    and existing_outbox.session_id == receiver_session_id
                    and str(existing_outbox.user_id or "")
                    == str(dialogue.owner_user_id)
                    and str(existing_outbox.client_message_key or "") == canonical_key
                    and str(existing_outbox.request_fingerprint or "")
                    == continuation_request_fingerprint
                )
                outbox_payload_marker_matches = (
                    outbox_payload_is_mapping
                    and bool(outbox_payload.get("_dialogue_human_input"))
                    and str(outbox_payload.get("persisted_user_message_id"))
                    == str(answer_uuid)
                    and isinstance(outbox_marker, Mapping)
                    and outbox_marker == existing_marker
                )
                outbox_finalized_matches = (
                    outbox_identity_matches
                    and str(existing_outbox.status or "") == "delivered"
                    and existing_outbox.lease_owner is None
                    and existing_outbox.lease_token is None
                    and existing_outbox.lease_expires_at is None
                    and outbox_payload_is_mapping
                    and outbox_payload == {}
                )
                outbox_inflight_matches = (
                    outbox_identity_matches
                    and str(existing_outbox.status or "") in {"pending", "claimed"}
                    and outbox_payload_marker_matches
                    and (
                        (
                            str(existing_outbox.status or "") == "pending"
                            and existing_outbox.lease_owner is None
                            and existing_outbox.lease_token is None
                            and existing_outbox.lease_expires_at is None
                        )
                        or (
                            str(existing_outbox.status or "") == "claimed"
                            and bool(str(existing_outbox.lease_owner or "").strip())
                            and bool(str(existing_outbox.lease_token or "").strip())
                            and existing_outbox.lease_expires_at is not None
                            and existing_outbox.lease_expires_at > datetime.utcnow()
                        )
                    )
                )
                terminal_answer_identity_matches = (
                    bool(terminal_answer_refs)
                    and str(existing_answer.get("session_id"))
                    == str(receiver_session_id)
                    and str(existing_answer.get("owner_user_id"))
                    == str(dialogue.owner_user_id)
                    and str(existing_answer.get("body_digest"))
                    == answer_body_digest
                    and str(existing_answer.get("authority_digest") or "")
                    == self._dialogue_digest(terminal_answer_refs)
                    and isinstance(existing_marker, Mapping)
                    and isinstance(existing_marker.get("source_human"), Mapping)
                    and existing_marker.get("source_human")
                    == {
                        **dict(source_human_anchor["refs"]),
                        "body_digest": source_human_anchor["body_digest"],
                    }
                    and str(existing_marker.get("source_human_authority_digest") or "")
                    == str(source_human_anchor["authority_digest"])
                    and str(existing_marker.get("authority_digest") or "")
                    == str(source_human_anchor["authority_digest"])
                )
                terminal_root_proven = (
                    root_provenance_matches
                    and terminal_root_fields_matches
                    and str(existing_root.status or "") == "succeeded"
                    and outbox_identity_matches
                    and terminal_answer_identity_matches
                )
                if (
                    terminal_root_proven
                    and (terminal_work_identity_matches or active_root_work_identity_matches)
                    and (outbox_finalized_matches or outbox_inflight_matches)
                    and not (
                        terminal_work_identity_matches and outbox_finalized_matches
                    )
                ):
                    # The Run has committed success before its linked
                    # interactive Work releases its valid provider lease.
                    # Defer the notification and refund the transport attempt
                    # until that exact Work reaches its terminal state.
                    from .agent_work_runtime import InteractiveAdmissionError

                    raise InteractiveAdmissionError("interactive_session_busy")
                if terminal_root_proven and terminal_work_identity_matches and outbox_finalized_matches:
                    # The finite Human Answer Root already completed.  A late
                    # worker question is only a notification receipt; reuse
                    # the exact terminal identity and never call the dispatch
                    # creator (which could repair/mint another Work).
                    return {
                        "id": str(existing_root.id),
                        "run_id": str(existing_root.id),
                        "receiver_run_id": str(existing_root.id),
                        "work_item_id": str(getattr(existing_root, "work_item_id")),
                        "receiver_work_item_id": str(getattr(existing_root, "work_item_id")),
                        "message_id": str(answer_uuid),
                        "receiver_message_id": str(answer_uuid),
                        "session_id": str(receiver_session_id),
                        "user_id": str(dialogue.owner_user_id),
                        "agent_id": str(receiver_agent_id),
                        "agent_revision_id": str(receiver_revision_id),
                        "parent_run_id": None,
                        "created": False,
                        "notification_only": True,
                        "notification_receipt_ack": True,
                        "response_digest": answer_body_digest,
                        "request_fingerprint": continuation_request_fingerprint,
                    }
                if not root_active_identity_matches:
                    raise AgentChatAdmissionError(
                        "Human answer Root outbox binding is invalid",
                        code="dialogue_answer_root_binding_invalid",
                    )
        sender_name = str(
            getattr(source_message, "sender_display_name", None) or "Agent"
        )[:160]
        result, message_id, created = await self.create_or_get_dispatch_turn(
            session_id=str(receiver_session_id),
            client_message_id=(
                continuation_client_message_id
                or f"agent-dialogue:{delivery_id}"
            ),
            content=str(content),
            message_metadata={"agent_dialogue": _jsonable(refs)},
            sender_type="agent",
            sender_id=str(source_agent_id),
            sender_display_name=sender_name,
            edit_message_id=None,
            outbox_payload={
                "_dialogue_transport": True,
                "agent_dialogue": _jsonable(refs),
                "context_project_hint_id": (
                    str(receiver_context_project_hint_id)
                    if receiver_context_project_hint_id
                    else None
                ),
                "persisted_user_message_id": None,
            },
            request_fingerprint=continuation_request_fingerprint,
            user_id=str(value(dialogue, "owner_user_id")),
            project_id=str(receiver_project_id) if receiver_project_id else None,
            context_project_hint_id=(
                str(receiver_context_project_hint_id)
                if receiver_context_project_hint_id
                else None
            ),
            task_id=str(value(dialogue, "task_id")) if value(dialogue, "task_id") else None,
            objective="agent_dialogue_delivery",
            metadata={"agent_dialogue": _jsonable(refs)},
            agent_id=str(receiver_agent_id),
            agent_revision_id=str(receiver_revision_id),
            expected_agent_id=str(receiver_agent_id),
            expected_agent_provided=True,
            session=session,
        )
        receiver_run_id = str(result.get("id") or "")
        receiver_work_item_id = result.get("work_item_id")
        if created and receiver_run_id and message_id:
            receiver_message = await session.get(
                ConversationMessage,
                parse_uuid(message_id),
            )
            if receiver_message is not None:
                receiver_metadata = getattr(receiver_message, "message_metadata", None)
                message_metadata = (
                    dict(receiver_metadata)
                    if isinstance(receiver_metadata, Mapping)
                    else {}
                )
                message_metadata["agent_run_id"] = receiver_run_id
                receiver_message.message_metadata = _jsonable(message_metadata)
                flush = getattr(session, "flush", None)
                if callable(flush):
                    await flush()
        return {
            **result,
            "receiver_run_id": receiver_run_id,
            "receiver_work_item_id": receiver_work_item_id,
            "receiver_message_id": str(message_id),
            "created": bool(created),
            "parent_run_id": None,
            "session_id": str(receiver_session_id),
            "user_id": str(value(dialogue, "owner_user_id")),
            "agent_id": str(receiver_agent_id),
            "agent_revision_id": str(receiver_revision_id),
        }

    async def hydrate_agent_dialogue_callback_payload(
        self,
        *,
        run_id: str,
        payload: Mapping[str, Any],
        lease_proof: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Hydrate an internal AgentDialogue callback from canonical rows.

        Dialogue outboxes intentionally retain only routing flags and typed
        references.  Recovery must re-read the encrypted trigger message,
        owner, receiver Run/Work and source-Human authority in one read-only
        session before handing the callback to ChatMessageMixin.
        """

        hydrated = dict(payload or {})
        if not hydrated.get("_dialogue_transport"):
            return hydrated
        if not isinstance(lease_proof, Mapping):
            raise AgentChatAdmissionError(
                "dialogue callback outbox lease proof is missing",
                code="dialogue_callback_lease_proof_missing",
            )
        run_uuid = parse_uuid(run_id)
        if run_uuid is None:
            raise AgentChatAdmissionError(
                "dialogue callback Run identity is invalid",
                code="dialogue_callback_run_invalid",
            )
        db_session = await self._session()
        try:
            outbox = await db_session.get(ConversationDispatchOutbox, run_uuid)
            proof_token = str(lease_proof.get("lease_token") or "")
            proof_owner = str(lease_proof.get("lease_owner") or "")
            proof_run_id = parse_uuid(lease_proof.get("run_id") or run_id)
            proof_expires_at = lease_proof.get("lease_expires_at")
            if isinstance(proof_expires_at, str):
                try:
                    proof_expires_at = datetime.fromisoformat(
                        proof_expires_at.replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                except ValueError:
                    proof_expires_at = None
            if (
                outbox is None
                or proof_run_id != run_uuid
                or str(outbox.status or "") != "claimed"
                or not proof_token
                or str(outbox.lease_token or "") != proof_token
                or not proof_owner
                or str(outbox.lease_owner or "") != proof_owner
                or proof_expires_at is None
                or outbox.lease_expires_at is None
                or outbox.lease_expires_at <= datetime.utcnow()
                or outbox.lease_expires_at != proof_expires_at
            ):
                raise AgentChatAdmissionError(
                    "dialogue callback outbox lease proof is stale",
                    code="dialogue_callback_lease_proof_invalid",
                )
            run = await db_session.get(AgentRun, run_uuid)
            if run is None or run.trigger_message_id is None or run.session_id is None:
                raise AgentChatAdmissionError(
                    "dialogue callback Run is incomplete",
                    code="dialogue_callback_run_invalid",
                )
            message = await db_session.get(ConversationMessage, run.trigger_message_id)
            marker = hydrated.get("agent_dialogue")
            if not isinstance(marker, Mapping):
                raise AgentChatAdmissionError(
                    "dialogue callback provenance is missing",
                    code="dialogue_callback_provenance_missing",
                )
            dialogue_uuid = parse_uuid(marker.get("dialogue_id"))
            source_agent_uuid = parse_uuid(marker.get("source_agent_id"))
            if dialogue_uuid is None or source_agent_uuid is None:
                raise AgentChatAdmissionError(
                    "dialogue callback provenance is incomplete",
                    code="dialogue_callback_provenance_incomplete",
                )
            dialogue = await db_session.get(AgentDialogue, dialogue_uuid)
            if dialogue is None:
                raise AgentChatAdmissionError(
                    "dialogue callback Dialogue is unavailable",
                    code="dialogue_callback_dialogue_missing",
                )
            dialogue_owner_uuid = parse_uuid(getattr(dialogue, "owner_user_id", None))
            if dialogue_owner_uuid is None:
                raise AgentChatAdmissionError(
                    "dialogue callback Dialogue owner is invalid",
                    code="dialogue_callback_owner_invalid",
                )
            delivery_uuid = parse_uuid(marker.get("delivery_id"))
            delivery = (
                await db_session.get(AgentDialogueDelivery, delivery_uuid)
                if delivery_uuid is not None
                else None
            )
            if delivery is None or delivery.dialogue_id != dialogue.id:
                raise AgentChatAdmissionError(
                    "dialogue callback Delivery is unavailable",
                    code="dialogue_callback_delivery_missing",
                )
            if str(delivery.state or "").casefold() in {
                "failed",
                "cancelled",
                "expired",
            }:
                raise AgentChatAdmissionError(
                    "dialogue callback Delivery is terminal",
                    code="dialogue_callback_delivery_terminal",
                )
            if delivery.receiver_run_id not in (None, run.id):
                raise AgentChatAdmissionError(
                    "dialogue callback receiver Run mismatch",
                    code="dialogue_callback_receiver_run_invalid",
                )
            if int(delivery.expected_control_revision or 0) != int(
                dialogue.control_revision or 0
            ):
                raise DispatchConflictError(
                    "dialogue callback control revision is stale",
                    code="dialogue_control_revision_changed",
                )
            if delivery.receiver_session_id != run.session_id or delivery.receiver_agent_id != run.agent_id:
                raise AgentChatAdmissionError(
                    "dialogue callback receiver binding is invalid",
                    code="dialogue_callback_receiver_invalid",
                )
            if run.agent_revision_id != delivery.receiver_agent_revision_id:
                raise AgentChatAdmissionError(
                    "dialogue callback receiver revision is invalid",
                    code="dialogue_callback_receiver_revision_invalid",
                )
            if str(marker.get("delivery_id") or "") != str(delivery.id):
                raise AgentChatAdmissionError(
                    "dialogue callback Delivery identity changed",
                    code="dialogue_callback_delivery_invalid",
                )
            for marker_key, delivery_value in (
                ("source_agent_id", delivery.source_agent_id),
                ("source_agent_revision_id", delivery.source_agent_revision_id),
                ("source_run_id", delivery.source_run_id),
                ("source_session_id", delivery.source_session_id),
                ("source_work_item_id", delivery.source_work_item_id),
            ):
                marker_value = marker.get(marker_key)
                if marker_value not in (None, "") and str(marker_value) != str(delivery_value):
                    raise AgentChatAdmissionError(
                        "dialogue callback source provenance changed",
                        code="dialogue_callback_source_invalid",
                    )
            receiver_role = str(
                marker.get("receiver_role") or marker.get("role") or ""
            ).strip().casefold()
            if receiver_role not in {"root", "worker"}:
                if delivery.receiver_session_id == dialogue.worker_session_id:
                    receiver_role = "worker"
                elif delivery.receiver_session_id == dialogue.root_session_id:
                    receiver_role = "root"
                else:
                    raise AgentChatAdmissionError(
                        "dialogue callback receiver direction is invalid",
                        code="dialogue_callback_receiver_invalid",
                    )
            expected_receiver_agent = (
                dialogue.worker_agent_id
                if receiver_role == "worker"
                else dialogue.root_agent_id
            )
            expected_receiver_session = (
                dialogue.worker_session_id
                if receiver_role == "worker"
                else dialogue.root_session_id
            )
            expected_source_agent = (
                dialogue.root_agent_id
                if receiver_role == "worker"
                else dialogue.worker_agent_id
            )
            if (
                run.agent_id != expected_receiver_agent
                or run.session_id != expected_receiver_session
                or delivery.source_agent_id != expected_source_agent
            ):
                raise AgentChatAdmissionError(
                    "dialogue callback direction binding is invalid",
                    code="dialogue_callback_direction_invalid",
                )
            source_run = (
                await db_session.get(AgentRun, delivery.source_run_id)
                if delivery.source_run_id is not None
                else None
            )
            source_work = (
                await db_session.get(AgentWorkItem, delivery.source_work_item_id)
                if delivery.source_work_item_id is not None
                else None
            )
            source_message = (
                await db_session.get(ConversationMessage, delivery.source_message_id)
                if delivery.source_message_id is not None
                else None
            )
            source_revision = await db_session.get(
                AgentRevision,
                delivery.source_agent_revision_id,
            )
            receiver_revision = await db_session.get(
                AgentRevision,
                delivery.receiver_agent_revision_id,
            )
            source_run_status = str(getattr(source_run, "status", "") or "").casefold()
            source_run_identity = getattr(source_run, "id", None)
            source_work_state = str(getattr(source_work, "state", "") or "").casefold()
            source_work_active_run_id = parse_uuid(
                getattr(source_work, "active_agent_run_id", None)
            )
            source_work_lease_active = bool(
                getattr(source_work, "lease_owner", None)
                and getattr(source_work, "lease_token", None)
                and getattr(source_work, "lease_expires_at", None) is not None
                and source_work.lease_expires_at > datetime.utcnow()
            )
            # Reuse the transport's canonical terminal predicate so the
            # durable failed-Run/blocked-Work question pause is accepted as a
            # settled source, while ordinary mismatched failures remain
            # rejected. Import lazily to avoid the service module cycle.
            from .agent_dialogue_transport import _run_work_state_matches

            source_work_settled = (
                source_run_status in RUN_TERMINAL_STATUSES
                and _run_work_state_matches(source_run, source_work)
                and source_work_active_run_id is None
                and not getattr(source_work, "lease_owner", None)
                and not getattr(source_work, "lease_token", None)
                and getattr(source_work, "lease_expires_at", None) is None
            )
            source_work_active = (
                source_work_active_run_id == source_run_identity
                and source_run_status not in RUN_TERMINAL_STATUSES
                and source_work_state in {"pending", "claimed", "running"}
                and source_work_lease_active
            )
            if (
                source_run is None
                or source_run.session_id != delivery.source_session_id
                or source_run.agent_id != delivery.source_agent_id
                or source_run.agent_revision_id != delivery.source_agent_revision_id
                or parse_uuid(run.user_id) is None
                or parse_uuid(run.user_id) != dialogue_owner_uuid
                or parse_uuid(source_run.user_id) is None
                or parse_uuid(source_run.user_id) != dialogue_owner_uuid
                or source_work is None
                or source_run.work_item_id != source_work.id
                or not (source_work_active or source_work_settled)
                or source_work.assigned_agent_id != source_run.agent_id
                or getattr(source_work, "conversation_session_id", None) not in (
                    None,
                    delivery.source_session_id,
                )
                or parse_uuid(getattr(source_work, "agent_revision_id", None)) is None
                or parse_uuid(getattr(source_run, "agent_revision_id", None)) is None
                or parse_uuid(getattr(delivery, "source_agent_revision_id", None)) is None
                or parse_uuid(getattr(source_work, "agent_revision_id", None))
                != parse_uuid(getattr(source_run, "agent_revision_id", None))
                or parse_uuid(getattr(source_work, "agent_revision_id", None))
                != parse_uuid(getattr(delivery, "source_agent_revision_id", None))
                or source_message is None
                or source_message.session_id != delivery.source_session_id
                or str(source_message.role or "").casefold() != "assistant"
                or str(source_message.sender_type or "").casefold() != "agent"
                or parse_uuid(source_message.sender_id) != delivery.source_agent_id
                or source_revision is None
                or source_revision.agent_id != delivery.source_agent_id
                or receiver_revision is None
                or receiver_revision.agent_id != delivery.receiver_agent_id
            ):
                raise AgentChatAdmissionError(
                    "dialogue callback source rows are not bound",
                    code="dialogue_callback_source_invalid",
                )
            owner_uuid = await self._require_dialogue_human_owner(
                db_session,
                run.user_id,
            )
            if (
                message is None
                or message.session_id != run.session_id
                or str(message.role or "").casefold() != "user"
                or str(message.sender_type or "").casefold() != "agent"
                or parse_uuid(message.sender_id) != delivery.source_agent_id
                or message.id != delivery.receiver_message_id
            ):
                raise AgentChatAdmissionError(
                    "dialogue callback Agent sender or receiver binding is invalid",
                    code="dialogue_callback_sender_invalid",
                )
            work = await db_session.get(AgentWorkItem, run.work_item_id) if run.work_item_id else None
            work_state = str(getattr(work, "state", "") or "").casefold()
            receiver_work_revision_uuid = parse_uuid(
                getattr(work, "agent_revision_id", None)
            )
            receiver_run_revision_uuid = parse_uuid(
                getattr(run, "agent_revision_id", None)
            )
            receiver_delivery_revision_uuid = parse_uuid(
                getattr(delivery, "receiver_agent_revision_id", None)
            )
            lease_active = bool(
                getattr(work, "lease_owner", None)
                and getattr(work, "lease_token", None)
                and getattr(work, "lease_expires_at", None) is not None
                and work.lease_expires_at > datetime.utcnow()
            )
            if (
                work is None
                or work.active_agent_run_id != run.id
                or work.assigned_agent_id != run.agent_id
                or work.conversation_session_id not in (None, run.session_id)
                or receiver_work_revision_uuid is None
                or receiver_run_revision_uuid is None
                or receiver_delivery_revision_uuid is None
                or receiver_work_revision_uuid != receiver_run_revision_uuid
                or receiver_work_revision_uuid != receiver_delivery_revision_uuid
                or work_state not in {"pending", "claimed", "running"}
                or (work_state in {"claimed", "running"} and not lease_active)
            ):
                raise AgentChatAdmissionError(
                    "dialogue callback Work binding is not current",
                    code="dialogue_callback_work_invalid",
                )
            source_human = await self._build_dialogue_human_anchor(
                db_session,
                source_run=run,
                source_work=work,
                include_body=False,
                dialogue=dialogue,
            )
            stored_authority = str(marker.get("authority_digest") or "").casefold()
            if stored_authority and stored_authority != str(source_human["authority_digest"]).casefold():
                raise AgentChatAdmissionError(
                    "dialogue callback Human authority changed",
                    code="dialogue_callback_authority_changed",
                )
            typed_resume_answer = False
            typed_resume_answer_message = None
            if marker.get("resume_answer") is True:
                input_uuid = parse_uuid(marker.get("input_id"))
                answer_uuid = parse_uuid(marker.get("source_human_message_id"))
                input_row = (
                    await db_session.get(AgentDialogueInput, input_uuid)
                    if input_uuid is not None
                    else None
                )
                resume_input_revision = marker.get("input_revision")
                resume_input_control_revision = marker.get(
                    "resume_input_control_revision"
                )
                answer_marker = marker.get("source_human_answer")
                all_inputs = (
                    await db_session.execute(
                        select(AgentDialogueInput).where(
                            AgentDialogueInput.dialogue_id == dialogue.id
                        )
                    )
                ).scalars().all()
                typed_resume_answer_message = (
                    await db_session.get(ConversationMessage, answer_uuid)
                    if answer_uuid is not None
                    else None
                )
                if (
                    input_row is None
                    or answer_uuid is None
                    or typed_resume_answer_message is None
                    or str(input_row.state or "").casefold() != "answered"
                    or input_row.dialogue_id != dialogue.id
                    or input_row.answer_message_id != answer_uuid
                    or resume_input_revision is None
                    or int(input_row.revision or 0) != int(resume_input_revision)
                    or resume_input_control_revision is None
                    or int(input_row.expected_control_revision or 0)
                    != int(resume_input_control_revision)
                    or any(
                        int(row.revision or 0) > int(resume_input_revision)
                        for row in all_inputs
                    )
                    or typed_resume_answer_message.session_id != dialogue.root_session_id
                    or str(typed_resume_answer_message.role or "").casefold()
                    != "user"
                    or str(typed_resume_answer_message.sender_type or "").casefold()
                    not in {"user", "human"}
                    or parse_uuid(typed_resume_answer_message.sender_id)
                    != dialogue.owner_user_id
                    or not isinstance(answer_marker, Mapping)
                    or str(answer_marker.get("message_id") or "")
                    != str(answer_uuid)
                    or str(answer_marker.get("body_digest") or "")
                    != hashlib.sha256(
                        str(typed_resume_answer_message.content or "").encode("utf-8")
                    ).hexdigest()
                ):
                    raise AgentChatAdmissionError(
                        "typed resume answer binding is invalid",
                        code="dialogue_typed_resume_answer_invalid",
                    )
                typed_resume_answer = True
            receiver_agent = await db_session.get(Agent, run.agent_id)
            receiver_name = str(getattr(receiver_agent, "display_name", "Agent") or "Agent")[:160]
            receiver_session = await db_session.get(ConversationSession, run.session_id)
            resolved_owner_uuid = parse_uuid(owner_uuid)
            receiver_session_owner_uuid = parse_uuid(
                getattr(receiver_session, "user_id", None)
            )
            if (
                resolved_owner_uuid is None
                or resolved_owner_uuid != dialogue_owner_uuid
                or receiver_session is None
                or receiver_session_owner_uuid is None
                or receiver_session_owner_uuid != dialogue_owner_uuid
            ):
                raise AgentChatAdmissionError(
                    "dialogue callback receiver Session owner is invalid",
                    code="dialogue_callback_receiver_owner_invalid",
                )
            context = receiver_session.context if receiver_session is not None else None
            context_hint = marker.get("context_project_hint_id")
            if context_hint in (None, "") and isinstance(context, Mapping):
                context_hint = context.get("context_project_hint_id")
            hydrated.update(
                {
                    # Keep the immutable receiver trigger as the callback
                    # message/persisted pointer.  TerminalMode consumes the
                    # separate trusted typed-resume projection for the
                    # provider prompt, preserving ChatMixin lineage checks.
                    "message": str(message.content or ""),
                    "session_id": str(run.session_id),
                    "project_id": str(run.project_id) if run.project_id else None,
                    "agent_run_id": str(run.id),
                    "persisted_user_message_id": str(run.trigger_message_id),
                    "skip_user_persistence": True,
                    "typed_resume_answer": typed_resume_answer,
                    "typed_resume_answer_body": (
                        str(typed_resume_answer_message.content or "")
                        if typed_resume_answer
                        else None
                    ),
                    "typed_resume_answer_id": (
                        str(typed_resume_answer_message.id)
                        if typed_resume_answer
                        else None
                    ),
                    "_sender_user_id": str(owner_uuid),
                    "_sender_display_name": "User",
                    "assistant_sender_type": "agent",
                    "assistant_sender_id": str(run.agent_id),
                    "assistant_sender_display_name": receiver_name,
                    "command_capabilities": list(
                        source_human["refs"].get("command_capabilities") or []
                    ),
                }
            )
            if context_hint not in (None, "") and not hydrated.get("project_id"):
                hydrated["context_project_hint_id"] = str(context_hint)
            return hydrated
        finally:
            await db_session.close()

    async def _supersede_unstarted_dialogue_question_root(
        self,
        db_session: Any,
        *,
        dialogue: Any,
        input_row: Any,
        answer_message_id: uuid.UUID,
    ) -> bool:
        """Replace one queued Root event handoff with the Human answer turn.

        ``agent_question`` remains eligible for the Root Agent to answer on
        its own. If the owner answers during the small admission window, the
        exact event-created Root Run must not turn the canonical Human turn
        into a generic session-busy conflict. Only that event identity may be
        superseded, and only before a provider has started.
        """

        from .agent_work_runtime import InteractiveAdmissionError

        expected_source = (
            parse_uuid(getattr(input_row, "source_run_id", None)),
            parse_uuid(getattr(input_row, "source_work_item_id", None)),
            parse_uuid(getattr(input_row, "source_message_id", None)),
        )
        if any(value is None for value in expected_source):
            # Historical answer rows may have been retained after their
            # nullable source references were cleaned up. They cannot prove
            # an event-created Root turn, so leave ordinary idempotent Human
            # dispatch unchanged; the strict event path above already rejects
            # nullable/foreign provenance before admission.
            return False
        active_runs = (
            await db_session.execute(
                select(AgentRun)
                .where(
                    AgentRun.session_id == dialogue.root_session_id,
                    AgentRun.run_type == "chat_turn",
                    AgentRun.status.in_(
                        ("queued", "running", "awaiting_approval", "uncertain")
                    ),
                )
                .with_for_update()
            )
        ).scalars().all()
        for root_run in active_runs:
            marker = (
                root_run.run_metadata.get("agent_dialogue")
                if isinstance(root_run.run_metadata, Mapping)
                else None
            )
            if not isinstance(marker, Mapping):
                continue
            marker_source = tuple(
                parse_uuid(marker.get(field))
                for field in (
                    "source_run_id",
                    "source_work_item_id",
                    "source_message_id",
                )
            )
            if (
                str(marker.get("dialogue_id") or "") != str(dialogue.id)
                or str(marker.get("role") or "").casefold() != "root"
                or str(marker.get("delivery_kind") or "").casefold() != "event"
                or marker_source != expected_source
            ):
                continue
            root_work = (
                await db_session.get(AgentWorkItem, root_run.work_item_id)
                if root_run.work_item_id is not None
                else None
            )
            delivery_id = parse_uuid(marker.get("delivery_id"))
            delivery = (
                await db_session.get(AgentDialogueDelivery, delivery_id)
                if delivery_id is not None
                else None
            )
            outbox = await db_session.get(ConversationDispatchOutbox, root_run.id)
            exact_work = (
                root_work is not None
                and root_work.active_agent_run_id == root_run.id
                and root_work.conversation_session_id == dialogue.root_session_id
                and root_work.assigned_agent_id == dialogue.root_agent_id
                and root_work.agent_revision_id == dialogue.root_agent_revision_id
                and str(root_work.source_type or "") == "chat_turn"
                and str(root_work.execution_adapter or "") == "interactive_chat"
                and str(root_work.state or "").casefold()
                in {"pending", "claimed", "running", "awaiting_approval", "uncertain"}
            )
            exact_delivery = (
                delivery is not None
                and delivery.dialogue_id == dialogue.id
                and delivery.source_run_id == expected_source[0]
                and delivery.source_work_item_id == expected_source[1]
                and delivery.source_message_id == expected_source[2]
                and delivery.receiver_session_id == dialogue.root_session_id
                and delivery.receiver_agent_id == dialogue.root_agent_id
                and delivery.receiver_agent_revision_id == dialogue.root_agent_revision_id
                and str(delivery.kind or "").casefold() == "event"
            )
            if not exact_work or not exact_delivery or outbox is None:
                raise AgentChatAdmissionError(
                    "dialogue Human answer event binding is invalid",
                    code="dialogue_answer_event_binding_invalid",
                )
            provider_started = bool(
                root_run.started_at
                or root_work.started_at
                or marker.get("provider_started") is True
            )
            if provider_started:
                raise InteractiveAdmissionError("dialogue_human_answer_deferred")
            now = datetime.utcnow()
            root_run.status = "cancelled"
            root_run.error = "dialogue_human_answer_superseded"
            root_run.result = {
                "outcome_classification": "cancelled",
                "reason": "human_answer_preempted_event_root",
                "dialogue_id": str(dialogue.id),
                "input_id": str(input_row.id),
                "answer_message_id": str(answer_message_id),
            }
            root_run.ended_at = root_run.ended_at or now
            root_run.updated_at = now
            root_work.state = "cancelled"
            root_work.outcome_classification = "cancelled"
            root_work.safe_error_code = "dialogue_human_answer_superseded"
            root_work.escalation_reason = "human answer superseded queued event Root turn"
            root_work.active_agent_run_id = None
            root_work.lease_owner = root_work.lease_token = root_work.lease_expires_at = None
            root_work.heartbeat_at = None
            root_work.cancelled_at = root_work.cancelled_at or now
            root_work.completed_at = root_work.completed_at or now
            root_work.updated_at = now
            outbox.status = "cancelled"
            outbox.lease_owner = outbox.lease_token = outbox.lease_expires_at = None
            outbox.updated_at = now
            delivery.state = "cancelled"
            delivery.updated_at = now
            return True
        return False

    async def hydrate_agent_dialogue_human_input_payload(
        self,
        *,
        run_id: str,
        payload: Mapping[str, Any],
        lease_proof: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Hydrate a pointer-only AoiDots Human root dispatch.

        Human answer and control outboxes deliberately persist only typed
        references.  The encrypted body remains in the canonical trigger
        ``ConversationMessage`` row.  Recovery must therefore prove the
        claimed outbox, root Run/Work, Dialogue/Input and sender/session
        identity before putting the body into the in-memory Chat callback.
        """

        hydrated = dict(payload or {})
        marker = hydrated.get("agent_dialogue")
        is_human_input = bool(hydrated.get("_dialogue_human_input"))
        is_human_control = bool(hydrated.get("_dialogue_human_control"))
        if is_human_input and is_human_control:
            raise AgentChatAdmissionError(
                "dialogue Human outbox kind is ambiguous",
                code="dialogue_human_payload_invalid",
            )
        if not (
            is_human_input
            or is_human_control
            or (
                isinstance(marker, Mapping)
                and marker.get("root_control_turn") is True
            )
        ):
            return hydrated
        if not isinstance(lease_proof, Mapping):
            raise AgentChatAdmissionError(
                "dialogue Human outbox lease proof is missing",
                code="dialogue_human_lease_proof_missing",
            )
        run_uuid = parse_uuid(run_id)
        if run_uuid is None:
            raise AgentChatAdmissionError(
                "dialogue Human Run identity is invalid",
                code="dialogue_human_run_invalid",
            )

        db_session = await self._session()
        try:
            outbox = await db_session.get(ConversationDispatchOutbox, run_uuid)
            proof_token = str(lease_proof.get("lease_token") or "")
            proof_owner = str(lease_proof.get("lease_owner") or "")
            proof_run_id = parse_uuid(lease_proof.get("run_id") or run_id)
            proof_expires_at = lease_proof.get("lease_expires_at")
            if isinstance(proof_expires_at, str):
                try:
                    proof_expires_at = datetime.fromisoformat(
                        proof_expires_at.replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                except ValueError:
                    proof_expires_at = None
            if (
                outbox is None
                or proof_run_id != run_uuid
                or str(outbox.status or "") != "claimed"
                or not proof_token
                or str(outbox.lease_token or "") != proof_token
                or not proof_owner
                or str(outbox.lease_owner or "") != proof_owner
                or proof_expires_at is None
                or outbox.lease_expires_at is None
                or outbox.lease_expires_at <= datetime.utcnow()
                or outbox.lease_expires_at != proof_expires_at
            ):
                raise AgentChatAdmissionError(
                    "dialogue Human outbox lease proof is stale",
                    code="dialogue_human_lease_proof_invalid",
                )

            durable_payload = outbox.payload if isinstance(outbox.payload, Mapping) else {}
            for key in (
                "_dialogue_human_input",
                "_dialogue_human_control",
                "control_action",
                "control_revision",
            ):
                if key in durable_payload and hydrated.get(key) != durable_payload.get(key):
                    raise AgentChatAdmissionError(
                        "dialogue Human outbox payload changed",
                        code="dialogue_human_payload_invalid",
                    )
            durable_marker = durable_payload.get("agent_dialogue")
            if not isinstance(marker, Mapping) or not isinstance(durable_marker, Mapping):
                raise AgentChatAdmissionError(
                    "dialogue Human provenance is missing",
                    code="dialogue_human_provenance_missing",
                )
            for key in (
                "dialogue_id",
                "role",
                "input_id",
                "input_revision",
                "control_action",
                "control_revision",
                "root_control_turn",
                "source_human_message_id",
                "source_human_answered",
                "source_human",
                "source_human_answer",
                "continuation_target",
                "authority_digest",
                "recovery_from_run_id",
                "resume_answer",
                "resume_input_control_revision",
            ):
                if key in durable_marker and marker.get(key) != durable_marker.get(key):
                    raise AgentChatAdmissionError(
                        "dialogue Human provenance changed",
                        code="dialogue_human_provenance_invalid",
                    )

            run = await db_session.get(AgentRun, run_uuid)
            if (
                run is None
                or run.trigger_message_id is None
                or run.session_id is None
                or run.user_id is None
                or run.agent_id is None
                or run.agent_revision_id is None
                or str(run.run_type or "") != "chat_turn"
                or str(run.status or "").casefold() not in RUN_ACTIVE_STATUSES
                or parse_uuid(run.root_run_id) != run.id
                or run.parent_run_id is not None
            ):
                raise AgentChatAdmissionError(
                    "dialogue Human root Run is not current",
                    code="dialogue_human_run_invalid",
                )
            dialogue_uuid = parse_uuid(marker.get("dialogue_id"))
            if dialogue_uuid is None:
                raise AgentChatAdmissionError(
                    "dialogue Human Dialogue identity is invalid",
                    code="dialogue_human_dialogue_invalid",
                )
            dialogue = await db_session.get(AgentDialogue, dialogue_uuid)
            if dialogue is None:
                raise AgentChatAdmissionError(
                    "dialogue Human Dialogue is unavailable",
                    code="dialogue_human_dialogue_missing",
                )
            owner_uuid = await self._require_dialogue_human_owner(
                db_session,
                dialogue.owner_user_id,
            )
            root_session = await db_session.get(ConversationSession, run.session_id)
            if (
                parse_uuid(run.user_id) != owner_uuid
                or str(outbox.user_id or "") != str(owner_uuid)
                or outbox.session_id != run.session_id
                or dialogue.root_session_id != run.session_id
                or dialogue.root_agent_id != run.agent_id
                or dialogue.root_agent_revision_id != run.agent_revision_id
                or root_session is None
                or root_session.deleted_at is not None
                or root_session.project_id is not None
                or parse_uuid(root_session.user_id) != owner_uuid
                or parse_uuid(root_session.agent_id) != run.agent_id
            ):
                raise AgentChatAdmissionError(
                    "dialogue Human root owner or Agent binding changed",
                    code="dialogue_human_binding_invalid",
                )
            stored_run_marker = (
                run.run_metadata.get("agent_dialogue")
                if isinstance(run.run_metadata, Mapping)
                else None
            )
            if not isinstance(stored_run_marker, Mapping):
                raise AgentChatAdmissionError(
                    "dialogue Human Run provenance is missing",
                    code="dialogue_human_provenance_missing",
                )
            for key in (
                "dialogue_id",
                "role",
                "input_id",
                "input_revision",
                "control_action",
                "control_revision",
                "root_control_turn",
                "source_human_message_id",
                "source_human_answered",
                "continuation_target",
                "recovery_from_run_id",
                "resume_answer",
                "resume_input_control_revision",
            ):
                if marker.get(key) != stored_run_marker.get(key):
                    raise AgentChatAdmissionError(
                        "dialogue Human Run provenance changed",
                        code="dialogue_human_provenance_invalid",
                    )
            if str(marker.get("role") or "").casefold() != "root":
                raise AgentChatAdmissionError(
                    "dialogue Human receiver direction is invalid",
                    code="dialogue_human_direction_invalid",
                )
            control_revision = int(dialogue.control_revision or 0)
            marker_revision = marker.get("control_revision")
            if marker_revision is None or int(marker_revision) != control_revision:
                raise DispatchConflictError(
                    "dialogue Human control revision is stale",
                    code="dialogue_control_revision_changed",
                )

            work_id = parse_uuid(run.work_item_id)
            work = await db_session.get(AgentWorkItem, work_id) if work_id else None
            if (
                work is None
                or parse_uuid(work.active_agent_run_id) != run.id
                or parse_uuid(work.conversation_session_id) != run.session_id
                or parse_uuid(work.assigned_agent_id) != run.agent_id
                or parse_uuid(work.agent_revision_id) != run.agent_revision_id
                or str(work.source_type or "") != "chat_turn"
                or str(work.source_id or "") != str(run.id)
                or str(work.source_revision or "") != str(run.agent_revision_id)
                or str(work.intent_key or "") != "interactive_chat"
                or str(work.execution_adapter or "") != "interactive_chat"
                or str(work.state or "").casefold()
                not in {"pending", "claimed", "running", "awaiting_approval"}
            ):
                raise AgentChatAdmissionError(
                    "dialogue Human Work binding is not current",
                    code="dialogue_human_work_invalid",
                )

            message = await db_session.get(ConversationMessage, run.trigger_message_id)
            if (
                message is None
                or message.session_id != run.session_id
                or str(message.role or "").casefold() != "user"
                or str(message.sender_type or "").casefold() not in {"user", "human"}
                or parse_uuid(message.sender_id) != owner_uuid
                or not str(message.content or "").strip()
            ):
                raise AgentChatAdmissionError(
                    "dialogue Human trigger message is invalid",
                    code="dialogue_human_message_invalid",
                )
            persisted_message_id = hydrated.get("persisted_user_message_id")
            if persisted_message_id not in (None, "") and parse_uuid(persisted_message_id) != message.id:
                raise AgentChatAdmissionError(
                    "dialogue Human persisted message pointer changed",
                    code="dialogue_human_message_invalid",
                )
            durable_persisted_message_id = durable_payload.get(
                "persisted_user_message_id"
            )
            if (
                durable_persisted_message_id not in (None, "")
                and parse_uuid(durable_persisted_message_id) != message.id
            ):
                raise AgentChatAdmissionError(
                    "dialogue Human durable message pointer changed",
                    code="dialogue_human_message_invalid",
                )
            if durable_payload.get("skip_user_persistence") is not True:
                raise AgentChatAdmissionError(
                    "dialogue Human persistence mode is invalid",
                    code="dialogue_human_payload_invalid",
                )
            message_metadata = (
                message.message_metadata
                if isinstance(message.message_metadata, Mapping)
                else {}
            )
            if parse_uuid(message_metadata.get("agent_dialogue_id")) != dialogue_uuid:
                raise AgentChatAdmissionError(
                    "dialogue Human message Dialogue binding is invalid",
                    code="dialogue_human_message_invalid",
                )

            if is_human_input:
                input_uuid = parse_uuid(marker.get("input_id"))
                input_row = (
                    await db_session.get(AgentDialogueInput, input_uuid)
                    if input_uuid is not None
                    else None
                )
                input_revision = marker.get("input_revision")
                answer_marker = marker.get("source_human_answer")
                input_source_run = (
                    await db_session.get(AgentRun, input_row.source_run_id)
                    if input_row is not None and input_row.source_run_id is not None
                    else None
                )
                input_source_work = (
                    await db_session.get(AgentWorkItem, input_row.source_work_item_id)
                    if input_row is not None and input_row.source_work_item_id is not None
                    else None
                )
                input_source_message = (
                    await db_session.get(ConversationMessage, input_row.source_message_id)
                    if input_row is not None and input_row.source_message_id is not None
                    else None
                )
                if (
                    input_row is None
                    or input_row.dialogue_id != dialogue.id
                    or str(input_row.state or "").casefold() != "answered"
                    or input_row.answer_message_id != message.id
                    or input_row.expected_control_revision != control_revision
                    or input_revision is None
                    or int(input_row.revision) != int(input_revision)
                    or parse_uuid(marker.get("source_human_message_id")) != message.id
                    or not isinstance(answer_marker, Mapping)
                    or parse_uuid(answer_marker.get("message_id")) != message.id
                    or parse_uuid(message_metadata.get("agent_dialogue_input_id")) != input_row.id
                    or int(message_metadata.get("input_revision", -1)) != int(input_row.revision)
                    or int(message_metadata.get("control_revision", -1)) != control_revision
                    or message_metadata.get("human_answer_source") is not True
                    or input_source_run is None
                    or parse_uuid(input_source_run.session_id) != parse_uuid(dialogue.worker_session_id)
                    or parse_uuid(input_source_run.agent_id) != parse_uuid(dialogue.worker_agent_id)
                    or parse_uuid(input_source_run.agent_revision_id) != parse_uuid(dialogue.worker_agent_revision_id)
                    or parse_uuid(input_source_run.user_id) != owner_uuid
                    or str(input_source_run.status or "").casefold()
                    not in {"failed", "succeeded", "cancelled"}
                    or input_source_work is None
                    or parse_uuid(input_source_run.work_item_id) != input_source_work.id
                    or parse_uuid(input_source_work.conversation_session_id) != parse_uuid(dialogue.worker_session_id)
                    or parse_uuid(input_source_work.assigned_agent_id) != parse_uuid(dialogue.worker_agent_id)
                    or parse_uuid(input_source_work.agent_revision_id) != parse_uuid(dialogue.worker_agent_revision_id)
                    or str(input_source_work.source_id or "") != str(input_source_run.id)
                    or str(input_source_work.source_revision or "")
                    != str(dialogue.worker_agent_revision_id)
                    or str(input_source_work.intent_key or "") != "interactive_chat"
                    or str(input_source_work.execution_adapter or "") != "interactive_chat"
                    or str(input_source_work.state or "").casefold()
                    not in {"blocked", "failed", "succeeded", "cancelled"}
                    or (
                        str(input_source_work.state or "").casefold()
                        in {"blocked", "failed", "succeeded", "cancelled"}
                        and input_source_work.active_agent_run_id is not None
                    )
                    or input_source_message is None
                    or input_source_message.id != input_row.source_message_id
                    or parse_uuid(input_source_message.session_id) != parse_uuid(dialogue.worker_session_id)
                    or str(input_source_message.role or "").casefold() != "assistant"
                    or str(input_source_message.sender_type or "").casefold() != "agent"
                    or parse_uuid(input_source_message.sender_id) != parse_uuid(dialogue.worker_agent_id)
                    or parse_uuid(
                        (input_source_message.message_metadata or {}).get("agent_dialogue_id")
                        if isinstance(input_source_message.message_metadata, Mapping)
                        else None
                    )
                    != dialogue.id
                    or parse_uuid(
                        (input_source_message.message_metadata or {}).get("agent_dialogue_input_id")
                        if isinstance(input_source_message.message_metadata, Mapping)
                        else None
                    )
                    != input_row.id
                ):
                    raise AgentChatAdmissionError(
                        "dialogue Human answer Input binding is invalid",
                        code="dialogue_human_input_invalid",
                    )
                if marker.get("source_human_answered") is not True:
                    raise AgentChatAdmissionError(
                        "dialogue Human continuation marker is missing",
                        code="dialogue_continuation_target_invalid",
                    )
                continuation_target = marker.get("continuation_target")
                expected_target = self._dialogue_continuation_target(
                    dialogue=dialogue,
                    input_row=input_row,
                    source_run=input_source_run,
                    source_work=input_source_work,
                    source_message=input_source_message,
                    answer_message_id=message.id,
                    root_run_id=run.id,
                    root_work_item_id=work.id,
                )
                if continuation_target != expected_target:
                    raise AgentChatAdmissionError(
                        "dialogue Human continuation target changed",
                        code="dialogue_continuation_target_invalid",
                    )
                hydrated["continuation_target"] = expected_target
            else:
                if (
                    marker.get("root_control_turn") is not True
                    or str(marker.get("control_action") or "").casefold()
                    not in {"resume", "rework", "instruction"}
                    or message_metadata.get("root_control_turn") is not True
                    or str(message_metadata.get("control_action") or "").casefold()
                    != str(marker.get("control_action") or "").casefold()
                    or int(message_metadata.get("control_revision", -1)) != control_revision
                ):
                    raise AgentChatAdmissionError(
                        "dialogue Human control binding is invalid",
                        code="dialogue_human_control_invalid",
                    )
                if marker.get("resume_answer") is True:
                    input_uuid = parse_uuid(marker.get("input_id"))
                    answer_uuid = parse_uuid(
                        marker.get("source_human_message_id")
                    )
                    input_row = (
                        await db_session.get(AgentDialogueInput, input_uuid)
                        if input_uuid is not None
                        else None
                    )
                    answer_message = (
                        await db_session.get(ConversationMessage, answer_uuid)
                        if answer_uuid is not None
                        else None
                    )
                    all_inputs = (
                        await db_session.execute(
                            select(AgentDialogueInput).where(
                                AgentDialogueInput.dialogue_id == dialogue.id
                            )
                        )
                    ).scalars().all()
                    answer_marker = marker.get("source_human_answer")
                    resume_input_control_revision = marker.get(
                        "resume_input_control_revision"
                    )
                    if (
                        input_row is None
                        or answer_message is None
                        or str(input_row.state or "").casefold() != "answered"
                        or input_row.dialogue_id != dialogue.id
                        or input_row.answer_message_id != answer_uuid
                        or int(input_row.revision or 0)
                        != int(marker.get("input_revision") or 0)
                        or int(input_row.expected_control_revision or 0)
                        != (
                            int(resume_input_control_revision)
                            if resume_input_control_revision is not None
                            else -1
                        )
                        or any(
                            int(row.revision or 0)
                            > int(marker.get("input_revision") or 0)
                            for row in all_inputs
                        )
                        or answer_message.session_id != dialogue.root_session_id
                        or str(answer_message.role or "").casefold() != "user"
                        or str(answer_message.sender_type or "").casefold()
                        not in {"user", "human"}
                        or parse_uuid(answer_message.sender_id) != owner_uuid
                        or not isinstance(answer_marker, Mapping)
                        or str(answer_marker.get("message_id") or "")
                        != str(answer_uuid)
                        or str(answer_marker.get("body_digest") or "")
                        != hashlib.sha256(
                            str(answer_message.content or "").encode("utf-8")
                        ).hexdigest()
                    ):
                        raise AgentChatAdmissionError(
                            "typed resume answer binding is invalid",
                            code="dialogue_typed_resume_answer_invalid",
                        )
                    hydrated["typed_resume_answer"] = True
                    hydrated["typed_resume_answer_body"] = str(
                        answer_message.content or ""
                    )
                    hydrated["typed_resume_answer_id"] = str(answer_uuid)

            source_human = await self._build_dialogue_human_anchor(
                db_session,
                source_run=run,
                source_work=work,
                include_body=False,
                dialogue=dialogue,
            )
            stored_authority = str(marker.get("authority_digest") or "").casefold()
            if stored_authority and stored_authority != str(source_human["authority_digest"]).casefold():
                raise AgentChatAdmissionError(
                    "dialogue Human authority changed",
                    code="dialogue_human_authority_changed",
                )
            hydrated.update(
                {
                    "message": str(message.content or ""),
                    "session_id": str(run.session_id),
                    "project_id": str(run.project_id) if run.project_id else None,
                    "agent_run_id": str(run.id),
                    "client_message_id": str(run.client_message_id or "") or None,
                    "persisted_user_message_id": str(message.id),
                    "skip_user_persistence": True,
                }
            )
            return hydrated
        finally:
            await db_session.close()

    async def pause_agent_dialogue_for_input(
        self,
        *,
        run_id: str,
        question: str,
        input_type: str = "free_text",
        choices: list[str] | None = None,
        allow_multiple: bool = False,
        allow_free_text: bool = True,
        input_kind: str = "agent_question",
        dialogue_id: str | None = None,
        expected_control_revision: int | None = None,
    ) -> Dict[str, Any] | None:
        """Persist a worker question and atomically block its Run/Work.

        This path deliberately does not call ``HumanInteractionManager``.  A
        process-local Future would keep the provider and root session alive,
        lose the question on restart, and allow a late worker continuation.
        """

        run_uuid = parse_uuid(run_id)
        dialogue_uuid = parse_uuid(dialogue_id)
        if run_uuid is None:
            raise ValueError("invalid dialogue worker run")
        db_session = await self._session()
        try:
            run = (
                await db_session.execute(
                    select(AgentRun).where(AgentRun.id == run_uuid).with_for_update()
                )
            ).scalars().first()
            if run is None or run.agent_id is None or run.session_id is None:
                raise AgentChatAdmissionError(
                    "dialogue worker Run is unavailable",
                    code="dialogue_worker_run_unavailable",
                )
            run_metadata = run.run_metadata if isinstance(run.run_metadata, dict) else {}
            dialogue_meta = run_metadata.get("agent_dialogue")
            if dialogue_uuid is None and isinstance(dialogue_meta, dict):
                dialogue_uuid = parse_uuid(dialogue_meta.get("dialogue_id"))
            if dialogue_uuid is None:
                raise AgentChatAdmissionError(
                    "dialogue identity is missing",
                    code="dialogue_identity_missing",
                )
            dialogue = (
                await db_session.execute(
                    select(AgentDialogue).where(AgentDialogue.id == dialogue_uuid).with_for_update()
                )
            ).scalars().first()
            if dialogue is None or dialogue.worker_session_id != run.session_id:
                raise AgentChatAdmissionError(
                    "dialogue worker binding mismatch",
                    code="dialogue_worker_binding_mismatch",
                )
            expected_revision = int(dialogue.control_revision or 0)
            if expected_control_revision is not None and int(expected_control_revision) != expected_revision:
                raise DispatchConflictError(
                    "dialogue control revision changed",
                    code="dialogue_control_revision_changed",
                )
            owner_uuid = await self._require_dialogue_human_owner(
                db_session,
                dialogue.owner_user_id,
            )
            if str(run.user_id or "") != str(owner_uuid):
                raise AgentChatAdmissionError(
                    "dialogue worker Run owner mismatch",
                    code="dialogue_worker_owner_mismatch",
                )
            if run.status in RUN_TERMINAL_STATUSES:
                existing_input = (
                    await db_session.execute(
                        select(AgentDialogueInput)
                        .where(
                            AgentDialogueInput.dialogue_id == dialogue_uuid,
                            AgentDialogueInput.source_run_id == run_uuid,
                            AgentDialogueInput.state == "pending",
                        )
                        .order_by(desc(AgentDialogueInput.revision))
                        .limit(1)
                    )
                ).scalars().first()
                if existing_input is not None:
                    await db_session.commit()
                    return {
                        "input_id": str(existing_input.id),
                        "dialogue_id": str(dialogue_uuid),
                        "run_id": str(run_uuid),
                        "message_id": str(existing_input.source_message_id) if existing_input.source_message_id else None,
                        "control_revision": expected_revision,
                    }
                raise AgentChatAdmissionError(
                    "dialogue worker Run is already terminal",
                    code="dialogue_worker_run_terminal",
                )
            work_id = parse_uuid(getattr(run, "work_item_id", None))
            if work_id is None:
                raise AgentChatAdmissionError(
                    "dialogue worker Work is missing",
                    code="dialogue_worker_work_missing",
                )
            work = (
                await db_session.execute(
                    select(AgentWorkItem).where(AgentWorkItem.id == work_id).with_for_update()
                )
            ).scalars().first()
            if work is None or work.active_agent_run_id not in (None, run_uuid):
                raise AgentChatAdmissionError(
                    "dialogue worker Work fence was lost",
                    code="dialogue_worker_work_fence_lost",
                )
            question_text = str(question or "").strip()
            if not question_text:
                raise ValueError("dialogue question is required")
            requested_input_kind = str(input_kind or "agent_question").strip().casefold()
            if requested_input_kind not in AGENT_DIALOGUE_INPUT_KINDS:
                raise AgentChatAdmissionError(
                    "dialogue input kind is not allowlisted",
                    code="dialogue_input_kind_invalid",
                )
            try:
                question_contract = build_agent_dialogue_question_contract(
                    input_type=input_type,
                    choices=choices,
                    allow_multiple=allow_multiple,
                    allow_free_text=allow_free_text,
                )
            except AgentDialogueAnswerError as exc:
                raise AgentChatAdmissionError(str(exc), code=exc.code) from exc
            normalized_type = question_contract.input_type
            bounded_choices = list(question_contract.choices)
            normalized_allow_multiple = question_contract.allow_multiple
            normalized_allow_free_text = question_contract.allow_free_text
            next_revision = int(
                await db_session.scalar(
                    select(func.max(AgentDialogueInput.revision)).where(
                        AgentDialogueInput.dialogue_id == dialogue_uuid
                    )
                )
                or 0
            ) + 1
            input_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"aoitalk:agent-dialogue-input:{run_uuid}:{expected_revision}:{next_revision}",
            )
            question_message_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"aoitalk:agent-dialogue-question:{input_id}",
            )
            existing = await db_session.get(AgentDialogueInput, input_id)
            if existing is not None:
                await db_session.commit()
                return {
                    "input_id": str(existing.id),
                    "dialogue_id": str(dialogue_uuid),
                    "run_id": str(run_uuid),
                    "message_id": str(existing.source_message_id) if existing.source_message_id else None,
                    "control_revision": expected_revision,
                }
            worker_agent = await db_session.get(Agent, run.agent_id)
            worker_name = str(getattr(worker_agent, "display_name", "Agent") or "Agent")[:160]
            now = datetime.utcnow()
            question_message = ConversationMessage(
                id=question_message_id,
                session_id=run.session_id,
                role="assistant",
                content=question_text,
                sender_type="agent",
                sender_id=str(run.agent_id),
                sender_display_name=worker_name,
                message_metadata=_jsonable(
                    {
                        "agent_dialogue_id": str(dialogue_uuid),
                        "agent_dialogue_input_id": str(input_id),
                        "agent_run_id": str(run_uuid),
                        "generation_status": "input_pending",
                        "safe_error_code": "dialogue_input_pending",
                        "input_type": normalized_type,
                        "choices": bounded_choices,
                        "allow_multiple": normalized_allow_multiple,
                        "allow_free_text": normalized_allow_free_text,
                        "input_kind": requested_input_kind,
                        "control_revision": expected_revision,
                    }
                ),
                created_at=now,
                updated_at=now,
            )
            db_session.add(question_message)
            db_session.add(
                AgentDialogueInput(
                    id=input_id,
                    dialogue_id=dialogue_uuid,
                    source_run_id=run_uuid,
                    source_work_item_id=work_id,
                    source_message_id=question_message_id,
                    input_kind=requested_input_kind,
                    reference_id=str(question_message_id),
                    expected_control_revision=expected_revision,
                    state="pending",
                    revision=next_revision,
                    created_at=now,
                    updated_at=now,
                )
            )
            run.status = "failed"
            run.error = "dialogue_input_pending"
            run.ended_at = now
            run.updated_at = now
            run_metadata = dict(run.run_metadata or {})
            run_metadata.update(
                {
                    "dialogue_input_id": str(input_id),
                    "dialogue_waiting": True,
                    "dialogue_control_revision": expected_revision,
                    "dialogue_id": str(dialogue_uuid),
                }
            )
            run.run_metadata = _safe_audit_metadata(run_metadata, run_type=run.run_type)
            await self._append_event(
                db_session,
                run,
                "run.failed",
                status="failed",
                message="dialogue_input_pending",
                payload={
                    "safe_error_code": "dialogue_input_pending",
                    "dialogue_input_id": str(input_id),
                    "dialogue_id": str(dialogue_uuid),
                    "control_revision": expected_revision,
                },
            )
            old_state = str(work.state or "")
            if old_state in {"succeeded", "failed", "cancelled", "dead_letter"}:
                raise AgentChatAdmissionError(
                    "dialogue worker Work is already terminal",
                    code="dialogue_worker_work_terminal",
                )
            work.state = "blocked"
            work.outcome_classification = "blocked"
            work.blocker_code = "dialogue_input_pending"
            work.safe_error_code = "dialogue_input_pending"
            work.escalation_reason = "dialogue input pending"
            work.lease_owner = None
            work.lease_token = None
            work.lease_expires_at = None
            work.heartbeat_at = now
            work.updated_at = now
            work.active_agent_run_id = None
            sequence = int(
                await db_session.scalar(
                    select(func.max(AgentWorkEvent.sequence)).where(
                        AgentWorkEvent.work_item_id == work.id
                    )
                )
                or 0
            ) + 1
            db_session.add(
                AgentWorkEvent(
                    id=uuid.uuid4(),
                    work_item_id=work.id,
                    sequence=sequence,
                    event_type="work.blocked",
                    from_state=old_state,
                    to_state="blocked",
                    actor_kind="service",
                    actor_id="aoitalk.dialogue",
                    actor_service_key="aoitalk.dialogue",
                    agent_run_id=run_uuid,
                    safe_error_code="dialogue_input_pending",
                    message="dialogue input pending",
                    payload_json={
                        "dialogue_id": str(dialogue_uuid),
                        "input_id": str(input_id),
                        "control_revision": expected_revision,
                    },
                    created_at=now,
                )
            )
            dialogue.state = "waiting"
            dialogue.updated_at = now
            await db_session.execute(
                update(ConversationSession)
                .where(ConversationSession.id == run.session_id)
                .values(development_status="waiting_for_user")
            )
            await db_session.commit()
            return {
                "input_id": str(input_id),
                "dialogue_id": str(dialogue_uuid),
                "run_id": str(run_uuid),
                "work_item_id": str(work_id),
                "message_id": str(question_message_id),
                "control_revision": expected_revision,
            }
        except Exception:
            await db_session.rollback()
            raise
        finally:
            await db_session.close()

    async def create_input_request(
        self,
        *,
        dialogue_id: str,
        source_run_id: str,
        source_work_item_id: str | None = None,
        source_message_id: str | None = None,
        question: str | None = None,
        input_kind: str = "agent_question",
        expected_control_revision: int,
    ) -> Dict[str, Any]:
        """Typed input seam for Service; Transport does not own Input state."""

        if not str(question or "").strip() and source_message_id:
            db_session = await self._session()
            try:
                source_message = await db_session.get(
                    ConversationMessage,
                    parse_uuid(source_message_id),
                )
                if source_message is not None:
                    question = str(source_message.content or "").strip()
            finally:
                await db_session.close()
        if not str(question or "").strip():
            raise AgentChatAdmissionError(
                "typed input requires an encrypted question body",
                code="dialogue_input_question_required",
            )
        if input_kind not in AGENT_DIALOGUE_INPUT_KINDS:
            raise AgentChatAdmissionError(
                "dialogue input kind is not allowlisted",
                code="dialogue_input_kind_invalid",
            )
        return dict(
            await self.pause_agent_dialogue_for_input(
                run_id=source_run_id,
                dialogue_id=dialogue_id,
                question=question,
                input_type=input_kind,
                expected_control_revision=expected_control_revision,
            )
            or {}
        )

    async def advance_control_revision(
        self,
        *,
        dialogue_id: str,
        state: str,
        expected_control_revision: int,
    ) -> Dict[str, Any]:
        """Typed control seam for Service; Delivery sequence is Bridge-owned."""

        db_session = await self._session()
        try:
            dialogue = await db_session.get(AgentDialogue, parse_uuid(dialogue_id))
            if dialogue is None:
                raise AgentChatAdmissionError("dialogue not found", code="dialogue_not_found")
            owner_id = str(dialogue.owner_user_id)
        finally:
            await db_session.close()
        action = "stop" if str(state or "").casefold() in {"cancelled", "stopped"} else "instruction"
        return await self.apply_agent_dialogue_control(
            dialogue_id=dialogue_id,
            owner_user_id=owner_id,
            action=action,
            expected_control_revision=expected_control_revision,
            actor_kind="human",
            human_http=True,
        )

    async def prepare_agent_dialogue_input_answer(
        self,
        *,
        input_id: str,
        answer: str,
        owner_user_id: str,
        dialogue_id: str | None = None,
        expected_control_revision: int | None = None,
        expected_input_revision: int | None = None,
        client_message_id: str | None = None,
    ) -> Dict[str, Any]:
        """Persist one exact human answer as a new finite-turn source row."""

        input_uuid = parse_uuid(input_id)
        dialogue_uuid = parse_uuid(dialogue_id)
        owner_hint_uuid = parse_uuid(owner_user_id)
        if input_uuid is None or owner_hint_uuid is None:
            raise ValueError("invalid dialogue answer identity")
        if expected_input_revision is None or int(expected_input_revision) < 1:
            raise AgentChatAdmissionError(
                "dialogue input revision is required",
                code="dialogue_input_revision_required",
            )
        expected_input_revision = int(expected_input_revision)
        answer_text = str(answer or "").strip()
        if not answer_text:
            raise ValueError("dialogue answer is required")
        db_session = await self._session()
        try:
            input_row = (
                await db_session.execute(
                    select(AgentDialogueInput).where(AgentDialogueInput.id == input_uuid).with_for_update()
                )
            ).scalars().first()
            if input_row is None:
                raise AgentChatAdmissionError("dialogue input not found", code="dialogue_input_not_found")
            if dialogue_uuid is not None and input_row.dialogue_id != dialogue_uuid:
                raise AgentChatAdmissionError("dialogue input mismatch", code="dialogue_input_mismatch")
            dialogue_uuid = input_row.dialogue_id
            dialogue = (
                await db_session.execute(
                    select(AgentDialogue).where(AgentDialogue.id == dialogue_uuid).with_for_update()
                )
            ).scalars().first()
            if dialogue is None:
                raise AgentChatAdmissionError("dialogue not found", code="dialogue_not_found")
            if int(input_row.revision or 0) != expected_input_revision:
                raise DispatchConflictError(
                    "dialogue input revision is stale",
                    code="dialogue_input_revision_stale",
                )
            owner_uuid = await self._require_dialogue_human_owner(db_session, dialogue.owner_user_id)
            if owner_uuid != owner_hint_uuid:
                raise AgentChatAdmissionError("dialogue answer owner mismatch", code="dialogue_answer_owner_mismatch")
            question_message_uuid = parse_uuid(
                input_row.source_message_id or input_row.reference_id
            )
            question_message = (
                await db_session.get(ConversationMessage, question_message_uuid)
                if question_message_uuid is not None
                else None
            )
            if question_message is None or str(question_message.session_id) not in {
                str(dialogue.root_session_id),
                str(dialogue.worker_session_id),
            }:
                raise AgentChatAdmissionError(
                    "dialogue question source is unavailable",
                    code="dialogue_question_source_invalid",
                )
            if (
                str(question_message.role or "").casefold() != "assistant"
                or str(question_message.sender_type or "").casefold() != "agent"
            ):
                raise AgentChatAdmissionError(
                    "dialogue question source is not an Agent message",
                    code="dialogue_question_source_invalid",
                )
            try:
                question_body = str(question_message.content or "").strip()
            except Exception as exc:
                raise AgentChatAdmissionError(
                    "dialogue question body is unavailable",
                    code="dialogue_question_source_invalid",
                ) from exc
            if not question_body:
                raise AgentChatAdmissionError(
                    "dialogue question body is empty",
                    code="dialogue_question_body_missing",
                )
            question_metadata = (
                question_message.message_metadata
                if isinstance(question_message.message_metadata, Mapping)
                else {}
            )
            for field, expected in (
                ("agent_dialogue_id", dialogue.id),
                ("agent_dialogue_input_id", input_row.id),
            ):
                actual = question_metadata.get(field)
                if actual not in (None, "") and parse_uuid(actual) != expected:
                    raise AgentChatAdmissionError(
                        "dialogue question source binding is invalid",
                        code="dialogue_question_source_invalid",
                    )
            try:
                question_contract = question_contract_from_message(
                    question_message,
                    input_kind=input_row.input_kind,
                )
                answer_value = parse_agent_dialogue_answer(
                    answer_text,
                    question_contract,
                )
                answer_text = answer_value.canonical
                answer_display_text = answer_value.display_text
            except AgentDialogueAnswerError as exc:
                raise AgentChatAdmissionError(str(exc), code=exc.code) from exc
            current_revision = int(dialogue.control_revision or 0)
            expected_input_control_revision = int(input_row.expected_control_revision or 0)
            if expected_control_revision is not None and int(expected_control_revision) != current_revision:
                raise DispatchConflictError("dialogue control revision changed", code="dialogue_control_revision_changed")
            if expected_input_control_revision != current_revision:
                raise DispatchConflictError("dialogue input is stale", code="dialogue_input_stale")
            answer_digest = canonical_answer_digest(answer_value)
            if str(input_row.state or "") == "answered" and input_row.answer_message_id is not None:
                if str(input_row.answer_digest or "") != answer_digest:
                    raise DispatchConflictError(
                        "dialogue input was answered with different content",
                        code="dialogue_input_answer_conflict",
                    )
                return {
                    "input_id": str(input_row.id),
                    "dialogue_id": str(dialogue_uuid),
                    "answer_message_id": str(input_row.answer_message_id),
                    "worker_session_id": str(dialogue.worker_session_id),
                    "worker_agent_id": str(dialogue.worker_agent_id),
                    "worker_agent_revision_id": str(dialogue.worker_agent_revision_id),
                    "control_revision": current_revision,
                    "idempotent": True,
                }
            if str(input_row.state or "") != "pending":
                raise AgentChatAdmissionError("dialogue input is not pending", code="dialogue_input_not_pending")
            message_seed = str(client_message_id or f"dialogue-answer:{input_uuid}:{answer_digest}")
            answer_message_id = uuid.uuid5(uuid.NAMESPACE_URL, f"aoitalk:agent-dialogue-answer:{input_uuid}:{answer_digest}")
            existing_message = await db_session.get(ConversationMessage, answer_message_id)
            if existing_message is None:
                now = datetime.utcnow()
                db_session.add(
                    ConversationMessage(
                        id=answer_message_id,
                        # Human answers are authored in the AoiDots/root
                        # session.  The transport later delivers this exact
                        # message reference to the worker child session.
                        session_id=dialogue.root_session_id,
                        role="user",
                        content=answer_display_text,
                        client_message_id=str(message_seed)[:512],
                        sender_type="user",
                        sender_id=str(owner_uuid),
                        sender_display_name="User",
                        message_metadata=_jsonable(
                            {
                                "agent_dialogue_id": str(dialogue_uuid),
                                "agent_dialogue_input_id": str(input_uuid),
                                "input_revision": int(input_row.revision or 0),
                                "control_revision": current_revision,
                                "dialogue_answer": True,
                                # This is the canonical marker consumed by
                                # pointer-only Human root hydration. Keep the
                                # historical dialogue_answer flag as an audit
                                # alias, but do not make recovery infer the
                                # sender from that alias.
                                "human_answer_source": True,
                            }
                        ),
                        created_at=now,
                        updated_at=now,
                    )
                )
                # ``AgentDialogueInput.answer_message_id`` is a real FK, and
                # dialogue sessions deliberately use ``autoflush=False`` in
                # the API/runtime paths.  Flush the newly-added source row
                # before assigning that FK; otherwise SQLAlchemy may emit the
                # input UPDATE before the unrelated ConversationMessage
                # INSERT and PostgreSQL rejects the transaction.
                await db_session.flush()
            input_row.state = "answered"
            input_row.answer_message_id = answer_message_id
            input_row.answer_digest = answer_digest
            input_row.updated_at = datetime.utcnow()
            await db_session.commit()
            return {
                "input_id": str(input_uuid),
                "dialogue_id": str(dialogue_uuid),
                "answer_message_id": str(answer_message_id),
                    "worker_session_id": str(dialogue.worker_session_id),
                "worker_agent_id": str(dialogue.worker_agent_id),
                "worker_agent_revision_id": str(dialogue.worker_agent_revision_id),
                "control_revision": current_revision,
                "idempotent": False,
            }
        except Exception:
            await db_session.rollback()
            raise
        finally:
            await db_session.close()

    async def _lock_manual_resume_proof_chain_nowait(
        self,
        db_session: Any,
        *,
        dialogue: Any,
        canonical_client_id: str,
    ) -> bool:
        """Preflight every later recovery lock with PostgreSQL NOWAIT.

        ``apply_agent_dialogue_control`` already owns the Dialogue row.  The
        terminal callback owns the Root Session before it takes Run and then
        Dialogue, so waiting for an old Run here would form a real cycle.
        Acquire the later chain in bounded NOWAIT order and convert only
        PostgreSQL ``55P03`` contention into a typed retryable admission
        denial. Other database errors remain failures and are not hidden.
        """

        candidate_runs = (
            await db_session.execute(
                select(AgentRun)
                .where(
                    AgentRun.session_id == dialogue.root_session_id,
                    AgentRun.user_id == str(dialogue.owner_user_id),
                    AgentRun.client_message_id == canonical_client_id,
                )
                .order_by(AgentRun.created_at.asc(), AgentRun.id.asc())
            )
        ).scalars().all()
        # An answered Input was already selected by the caller. Any missing,
        # ambiguous, or stale historical Root therefore fails closed; the
        # ordinary resume path returns before this helper when no Input exists.
        if not candidate_runs:
            return False
        if len(candidate_runs) != 1:
            return False

        try:
            await db_session.execute(
                select(ConversationSession.id)
                .where(ConversationSession.id == dialogue.root_session_id)
                .with_for_update(nowait=True)
            )
            locked_run = (
                await db_session.execute(
                    select(AgentRun)
                    .where(AgentRun.id == candidate_runs[0].id)
                    .with_for_update(nowait=True)
                    .execution_options(populate_existing=True)
                )
            ).scalars().first()
            if locked_run is None:
                return False

            work_id = parse_uuid(getattr(locked_run, "work_item_id", None))
            if work_id is not None:
                await db_session.execute(
                    select(AgentWorkItem)
                    .where(AgentWorkItem.id == work_id)
                    .with_for_update(nowait=True)
                    .execution_options(populate_existing=True)
                )
            await db_session.execute(
                select(ConversationDispatchOutbox)
                .where(ConversationDispatchOutbox.run_id == locked_run.id)
                .with_for_update(nowait=True)
            )
            budget_root_id = parse_uuid(
                getattr(dialogue, "budget_root_dialogue_id", None)
            )
            if budget_root_id is not None:
                await db_session.execute(
                    select(AgentDialogueBudgetRoot)
                    .where(
                        AgentDialogueBudgetRoot.root_dialogue_id == budget_root_id
                    )
                    .with_for_update(nowait=True)
                )
            await db_session.execute(
                select(AgentDialogueUsage)
                .where(AgentDialogueUsage.run_id == locked_run.id)
                .with_for_update(nowait=True)
            )
            await db_session.execute(
                select(AgentDialogueReservation)
                .where(AgentDialogueReservation.run_id == locked_run.id)
                .with_for_update(nowait=True)
            )
        except OperationalError as exc:
            if _is_postgres_lock_not_available(exc):
                raise AgentChatAdmissionError(
                    "manual resume is deferred while the terminal callback settles",
                    code="dialogue_manual_resume_deferred",
                ) from exc
            raise
        return True

    async def _execute_manual_resume_lock_nowait(
        self,
        db_session: Any,
        statement: Any,
    ) -> Any:
        """Execute one resume-only later-row lock without waiting."""

        try:
            return await db_session.execute(
                statement.with_for_update(nowait=True)
            )
        except OperationalError as exc:
            if _is_postgres_lock_not_available(exc):
                raise AgentChatAdmissionError(
                    "manual resume is deferred while the terminal callback settles",
                    code="dialogue_manual_resume_deferred",
                ) from exc
            raise

    async def _prepare_manual_resume_answer_recovery(
        self,
        db_session: Any,
        *,
        dialogue: Any,
        expected_control_revision: int,
    ) -> dict[str, Any] | None:
        """Prove one answered Input can be carried by a manual resume.

        This runs while the Dialogue row is locked and is intentionally
        stricter than the ordinary same-revision automatic recovery.  A
        manual resume may advance exactly one control fence, only when there
        is one answered Input at the current revision and no newer Input.
        """

        current_revision = int(expected_control_revision)
        # Dialogue is already locked by apply_agent_dialogue_control.  Keep
        # this read in the Dialogue→proof order; answer CAS/dispatch uses
        # Input→Dialogue and taking an Input lock here would deadlock it.
        inputs = (
            await db_session.execute(
                select(AgentDialogueInput).where(
                    AgentDialogueInput.dialogue_id == dialogue.id
                )
            )
        ).scalars().all()
        answered = [
            row
            for row in inputs
            if str(row.state or "").casefold() == "answered"
            and int(row.expected_control_revision or 0) == current_revision
            and row.answer_message_id is not None
        ]
        if not answered:
            return None
        if len(answered) != 1:
            raise AgentChatAdmissionError(
                "manual resume has ambiguous answered Inputs",
                code="dialogue_manual_resume_input_ambiguous",
            )
        input_row = answered[0]
        input_revision = int(input_row.revision or 0)
        if input_revision < 1 or any(
            int(row.revision or 0) > input_revision for row in inputs
        ):
            raise AgentChatAdmissionError(
                "manual resume answered Input is stale",
                code="dialogue_manual_resume_input_stale",
            )
        answer_id = parse_uuid(input_row.answer_message_id)
        if answer_id is None:
            raise AgentChatAdmissionError(
                "manual resume answer identity is invalid",
                code="dialogue_manual_resume_answer_invalid",
            )
        answer_message = (
            await db_session.execute(
                select(ConversationMessage)
                .where(ConversationMessage.id == answer_id)
            )
        ).scalars().first()
        owner_uuid = parse_uuid(dialogue.owner_user_id)
        if (
            answer_message is None
            or owner_uuid is None
            or answer_message.session_id != dialogue.root_session_id
            or str(answer_message.role or "").casefold() != "user"
            or str(answer_message.sender_type or "").casefold() not in {"user", "human"}
            or parse_uuid(answer_message.sender_id) != owner_uuid
        ):
            raise AgentChatAdmissionError(
                "manual resume answer message binding is invalid",
                code="dialogue_manual_resume_answer_invalid",
            )
        answer_metadata = (
            answer_message.message_metadata
            if isinstance(answer_message.message_metadata, Mapping)
            else {}
        )
        if (
            answer_metadata.get("human_answer_source") is not True
            or str(answer_metadata.get("agent_dialogue_id") or "")
            != str(dialogue.id)
            or str(answer_metadata.get("agent_dialogue_input_id") or "")
            != str(input_row.id)
            or int(answer_metadata.get("input_revision", -1)) != input_revision
            or int(answer_metadata.get("control_revision", -1)) != current_revision
        ):
            raise AgentChatAdmissionError(
                "manual resume answer metadata is invalid",
                code="dialogue_manual_resume_answer_invalid",
            )
        source_run_id = parse_uuid(input_row.source_run_id)
        source_work_id = parse_uuid(input_row.source_work_item_id)
        source_message_id = parse_uuid(input_row.source_message_id)
        if None in (source_run_id, source_work_id, source_message_id):
            raise AgentChatAdmissionError(
                "manual resume Input provenance is incomplete",
                code="dialogue_manual_resume_provenance_invalid",
            )
        source_run = await db_session.get(AgentRun, source_run_id)
        source_work = await db_session.get(AgentWorkItem, source_work_id)
        source_message = await db_session.get(ConversationMessage, source_message_id)
        if (
            source_run is None
            or source_work is None
            or source_message is None
            or source_run.session_id != dialogue.worker_session_id
            or source_run.agent_id != dialogue.worker_agent_id
            or source_run.agent_revision_id != dialogue.worker_agent_revision_id
            or source_run.work_item_id != source_work.id
            or source_work.conversation_session_id != dialogue.worker_session_id
            or source_work.assigned_agent_id != dialogue.worker_agent_id
            or source_work.agent_revision_id != dialogue.worker_agent_revision_id
            or source_message.session_id != dialogue.worker_session_id
            or str(source_message.role or "").casefold() != "assistant"
            or str(source_message.sender_type or "").casefold() != "agent"
            or parse_uuid(source_message.sender_id) != dialogue.worker_agent_id
        ):
            raise AgentChatAdmissionError(
                "manual resume source binding is invalid",
                code="dialogue_manual_resume_provenance_invalid",
            )
        canonical_client_id = (
            f"dialogue-human-answer:{input_row.id}:{input_revision}:{answer_id}"
        )
        proof_chain_state = await self._lock_manual_resume_proof_chain_nowait(
            db_session,
            dialogue=dialogue,
            canonical_client_id=canonical_client_id,
        )
        if proof_chain_state is not True:
            raise AgentChatAdmissionError(
                "manual resume old answer Root proof is unavailable",
                code="dialogue_manual_resume_proof_invalid",
            )
        from .agent_dialogue_transport import prove_failed_answer_root_recovery

        old_run_id = await prove_failed_answer_root_recovery(
            db_session,
            dialogue=dialogue,
            input_row=input_row,
            answer_message=answer_message,
            canonical_client_id=canonical_client_id,
        )
        if old_run_id is None:
            raise AgentChatAdmissionError(
                "manual resume old answer Root proof is unavailable",
                code="dialogue_manual_resume_proof_invalid",
            )
        return {
            "input_id": str(input_row.id),
            "input_revision": input_revision,
            "answer_message_id": str(answer_id),
            "source_run_id": str(source_run_id),
            "source_work_item_id": str(source_work_id),
            "source_message_id": str(source_message_id),
            "answer_message": answer_message,
            "recovery_from_run_id": str(old_run_id),
            "expected_control_revision": current_revision,
        }

    async def _create_dialogue_control_root_turn(
        self,
        db_session: Any,
        *,
        dialogue: AgentDialogue,
        owner_uuid: uuid.UUID,
        action: str,
        control_revision: int,
        instruction: str | None,
        answer_recovery: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Persist a finite Human control turn for the ordinary root Agent.

        Resume/rework must go through the root Agent's normal Run/Work/outbox
        path.  The old worker Run is never revived.  The original Human
        anchor is re-read from the Dialogue's canonical source Run and pinned
        by digest in the new root Run metadata; the synthetic control text is
        only a bounded directive for that finite root turn.
        """

        root_session = (
            await db_session.execute(
                select(ConversationSession)
                .where(
                    ConversationSession.id == dialogue.root_session_id,
                    ConversationSession.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalars().first()
        if root_session is None or root_session.project_id is not None:
            raise AgentChatAdmissionError(
                "dialogue root session must remain projectless",
                code="dialogue_root_project_forbidden",
            )
        root_agent_uuid = parse_uuid(root_session.agent_id)
        if root_agent_uuid is None or root_agent_uuid != parse_uuid(dialogue.root_agent_id):
            raise AgentChatAdmissionError(
                "dialogue root Agent binding changed",
                code="dialogue_root_agent_binding_changed",
            )

        human_run_uuid = parse_uuid(getattr(dialogue, "human_run_id", None))
        human_run = (
            await db_session.get(AgentRun, human_run_uuid)
            if human_run_uuid is not None
            else None
        )
        if human_run is None and getattr(dialogue, "human_message_id", None) is not None:
            human_message_uuid = parse_uuid(dialogue.human_message_id)
            if human_message_uuid is not None:
                human_run = (
                    await db_session.execute(
                        select(AgentRun)
                        .where(AgentRun.trigger_message_id == human_message_uuid)
                        .order_by(AgentRun.created_at.asc())
                        .limit(1)
                    )
                ).scalars().first()
        if human_run is None:
            raise AgentChatAdmissionError(
                "dialogue Human anchor is unavailable",
                code="dialogue_source_human_anchor_missing",
            )
        human_work = None
        human_work_uuid = parse_uuid(getattr(dialogue, "human_work_item_id", None))
        if human_work_uuid is not None:
            human_work = await db_session.get(AgentWorkItem, human_work_uuid)
        source_human = await self._build_dialogue_human_anchor(
            db_session,
            source_run=human_run,
            source_work=human_work,
            include_body=False,
            dialogue=dialogue,
        )
        recovery_marker: dict[str, Any] | None = None
        recovery_source_run = None
        recovery_source_work = None
        recovery_source_message = None
        recovery_input_row = None
        if answer_recovery is not None:
            answer_message = answer_recovery.get("answer_message")
            answer_id = parse_uuid(answer_recovery.get("answer_message_id"))
            if answer_message is None or answer_id is None:
                raise AgentChatAdmissionError(
                    "manual resume answer recovery is incomplete",
                    code="dialogue_manual_resume_answer_invalid",
                )
            recovery_source_run_id = parse_uuid(answer_recovery.get("source_run_id"))
            recovery_source_work_id = parse_uuid(
                answer_recovery.get("source_work_item_id")
            )
            recovery_source_message_id = parse_uuid(
                answer_recovery.get("source_message_id")
            )
            recovery_source_run = (
                await db_session.get(AgentRun, recovery_source_run_id)
                if recovery_source_run_id is not None
                else None
            )
            recovery_source_work = (
                await db_session.get(AgentWorkItem, recovery_source_work_id)
                if recovery_source_work_id is not None
                else None
            )
            recovery_source_message = (
                await db_session.get(ConversationMessage, recovery_source_message_id)
                if recovery_source_message_id is not None
                else None
            )
            recovery_input_row = await db_session.get(
                AgentDialogueInput,
                parse_uuid(answer_recovery.get("input_id")),
            )
            if (
                recovery_source_run is None
                or recovery_source_work is None
                or recovery_source_message is None
                or recovery_input_row is None
            ):
                raise AgentChatAdmissionError(
                    "manual resume continuation source is unavailable",
                    code="dialogue_continuation_target_invalid",
                )
            answer_refs = {
                **dict(source_human["refs"]),
                "message_id": str(answer_id),
                "session_id": str(root_session.id),
                "owner_user_id": str(owner_uuid),
            }
            answer_anchor = {
                **answer_refs,
                "body_digest": hashlib.sha256(
                    str(answer_message.content or "").encode("utf-8")
                ).hexdigest(),
                "authority_digest": self._dialogue_digest(answer_refs),
            }
            recovery_marker = {
                "input_id": str(answer_recovery["input_id"]),
                "input_revision": int(answer_recovery["input_revision"]),
                "source_human_message_id": str(answer_id),
                "source_human_answered": True,
                "source_human_answer": answer_anchor,
                "recovery_from_run_id": str(
                    answer_recovery["recovery_from_run_id"]
                ),
                "resume_answer": True,
                "resume_input_control_revision": int(
                    answer_recovery["expected_control_revision"]
                ),
            }
        normalized_instruction = str(instruction or "").strip()[:30_000]
        if not normalized_instruction:
            normalized_instruction = (
                "AgentDialogueの作業を再開してください。"
                "元の依頼と現在の許可範囲を確認し、新しいWorker実行を開始してください。"
                if action == "resume"
                else "AgentDialogueの作業をやり直してください。"
                "以前のWorker実行を再利用せず、元の依頼と現在の許可範囲を確認して"
                "新しいWorker実行を開始してください。"
            )
        control_digest = self._dialogue_digest(
            {
                "dialogue_id": str(dialogue.id),
                "action": action,
                "control_revision": control_revision,
                "instruction": normalized_instruction,
                "source_human_authority": source_human["authority_digest"],
                "resume_answer": recovery_marker,
            }
        )
        metadata_agent_dialogue = {
            "dialogue_id": str(dialogue.id),
            "role": "root",
            "control_action": action,
            "control_revision": control_revision,
            "root_control_turn": True,
            "source_human": {
                **dict(source_human["refs"]),
                "body_digest": source_human["body_digest"],
            },
            "authority_digest": source_human["authority_digest"],
        }
        if recovery_marker is not None:
            metadata_agent_dialogue.update(recovery_marker)
        metadata = {"agent_dialogue": metadata_agent_dialogue}
        root_run, trigger_id, created = await self.create_or_get_dispatch_turn(
            session_id=str(root_session.id),
            client_message_id=(
                f"dialogue-control-root:{dialogue.id}:{control_revision}:{action}"
            ),
            content=normalized_instruction,
            message_metadata={
                "agent_dialogue_id": str(dialogue.id),
                "control_action": action,
                "control_revision": control_revision,
                "root_control_turn": True,
                "source_human_authority_digest": source_human["authority_digest"],
            },
            sender_type="user",
            sender_id=str(owner_uuid),
            sender_display_name="User",
            edit_message_id=None,
            outbox_payload={
                "_dialogue_human_control": True,
                "agent_dialogue": _jsonable(metadata_agent_dialogue),
                "control_action": action,
                "control_revision": control_revision,
            },
            request_fingerprint=control_digest,
            user_id=str(owner_uuid),
            project_id=None,
            objective=normalized_instruction,
            metadata=metadata,
            agent_id=str(root_agent_uuid),
            expected_agent_id=str(root_agent_uuid),
            expected_agent_provided=True,
            context_project_hint_id=None,
            session=db_session,
        )
        control_refs = {
            "message_id": str(trigger_id),
            # Keep the original Human Run/Work trace as the authority scope;
            # the finite control Run gets its own explicit provenance key.
            "run_id": source_human["refs"]["run_id"],
            "work_item_id": source_human["refs"].get("work_item_id"),
            "owner_user_id": str(owner_uuid),
            "session_id": str(root_session.id),
            "binding_hash": source_human["refs"].get("binding_hash"),
            "manifest_hash": source_human["refs"].get("manifest_hash"),
            "command_capabilities": list(
                source_human["refs"].get("command_capabilities") or []
            ),
            "control_run_id": str(root_run.get("id")),
        }
        control_anchor = {
            **control_refs,
            "body_digest": hashlib.sha256(
                normalized_instruction.encode("utf-8")
            ).hexdigest(),
            "authority_digest": self._dialogue_digest(control_refs),
        }
        root_run_uuid = parse_uuid(root_run.get("id"))
        # The Human control trigger remains the immutable user-authored
        # message for the Root Run.  A Worker control Delivery requires a
        # separate assistant/Agent source message, so create an idempotent
        # Root-signed relay instead of rebinding or overwriting the Human row.
        root_agent_message_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"aoitalk:agent-dialogue-control-relay:{dialogue.id}:{control_revision}:{action}:{control_digest}",
        )
        root_agent_message = await db_session.get(
            ConversationMessage,
            root_agent_message_id,
        )
        root_run_row = (
            await db_session.get(AgentRun, root_run_uuid)
            if root_run_uuid is not None
            else None
        )
        root_work_id = parse_uuid(
            getattr(root_run_row, "work_item_id", None)
            or root_run.get("work_item_id")
        )
        continuation_target = None
        if (
            answer_recovery is not None
            and recovery_input_row is not None
            and recovery_source_run is not None
            and recovery_source_work is not None
            and recovery_source_message is not None
            and root_run_uuid is not None
            and root_work_id is not None
        ):
            continuation_target = self._dialogue_continuation_target(
                dialogue=dialogue,
                input_row=recovery_input_row,
                source_run=recovery_source_run,
                source_work=recovery_source_work,
                source_message=recovery_source_message,
                answer_message_id=answer_recovery.get("answer_message_id"),
                root_run_id=root_run_uuid,
                root_work_item_id=root_work_id,
            )
            recovery_marker["continuation_target"] = continuation_target
            persisted_outbox = await db_session.get(
                ConversationDispatchOutbox,
                root_run_uuid,
            )
            if persisted_outbox is None or not isinstance(
                persisted_outbox.payload, Mapping
            ):
                raise AgentChatAdmissionError(
                    "manual resume continuation outbox is unavailable",
                    code="dialogue_continuation_target_invalid",
                )
            stored_root_marker = (
                root_run_row.run_metadata.get("agent_dialogue")
                if root_run_row is not None
                and isinstance(root_run_row.run_metadata, Mapping)
                else None
            )
            stored_root_target = (
                stored_root_marker.get("continuation_target")
                if isinstance(stored_root_marker, Mapping)
                else None
            )
            stored_outbox_marker = (
                persisted_outbox.payload.get("agent_dialogue")
                if isinstance(persisted_outbox.payload, Mapping)
                else None
            )
            stored_outbox_target = (
                stored_outbox_marker.get("continuation_target")
                if isinstance(stored_outbox_marker, Mapping)
                else None
            )
            if not created:
                delivered_outbox_pruned = (
                    isinstance(persisted_outbox.payload, Mapping)
                    and not persisted_outbox.payload
                    and str(getattr(persisted_outbox, "status", "")).casefold()
                    == "delivered"
                    and str(getattr(root_run_row, "status", "")).casefold()
                    == "succeeded"
                )
                if (
                    stored_root_target != continuation_target
                    or (
                        stored_outbox_target != continuation_target
                        and not delivered_outbox_pruned
                    )
                ):
                    raise AgentChatAdmissionError(
                        "manual resume continuation target changed",
                        code="dialogue_continuation_target_invalid",
                    )
            else:
                if root_run_row is None or root_work_id is None:
                    raise AgentChatAdmissionError(
                        "manual resume continuation Root binding is unavailable",
                        code="dialogue_continuation_target_invalid",
                    )
                root_metadata = dict(root_run_row.run_metadata or {})
                root_dialogue_metadata = dict(
                    root_metadata.get("agent_dialogue") or {}
                )
                root_dialogue_metadata["continuation_target"] = continuation_target
                root_metadata["agent_dialogue"] = root_dialogue_metadata
                root_run_row.run_metadata = _safe_audit_metadata(
                    root_metadata,
                    run_type=root_run_row.run_type,
                )
                outbox_payload = dict(persisted_outbox.payload)
                outbox_marker = dict(outbox_payload.get("agent_dialogue") or {})
                outbox_marker["continuation_target"] = continuation_target
                outbox_payload["agent_dialogue"] = outbox_marker
                persisted_outbox.payload = outbox_payload
        if answer_recovery is not None and continuation_target is None:
            raise AgentChatAdmissionError(
                "manual resume continuation Root binding is unavailable",
                code="dialogue_continuation_target_invalid",
            )
        # Existing idempotent controls remain immutable.  New Root controls
        # receive their source Human provenance exactly once; retries only
        # verify the persisted marker above.
        if created and root_run_row is not None:
            root_metadata = dict(root_run_row.run_metadata or {})
            root_dialogue_metadata = dict(
                root_metadata.get("agent_dialogue") or {}
            )
            root_dialogue_metadata["source_human_control"] = control_anchor
            root_dialogue_metadata["source_human_control_authority_digest"] = (
                source_human["authority_digest"]
            )
            root_metadata["agent_dialogue"] = root_dialogue_metadata
            root_run_row.run_metadata = _safe_audit_metadata(
                root_metadata,
                run_type=root_run_row.run_type,
            )
            await db_session.flush()
        if root_agent_message is None:
            root_agent = await db_session.get(Agent, root_agent_uuid)
            root_agent_message = ConversationMessage(
                id=root_agent_message_id,
                session_id=root_session.id,
                role="assistant",
                content=normalized_instruction,
                client_message_id=(
                    f"dialogue-control-relay:{dialogue.id}:{control_revision}:{action}"
                ),
                sender_type="agent",
                sender_id=str(root_agent_uuid),
                sender_display_name=str(
                    getattr(root_agent, "display_name", None) or "Agent"
                )[:160],
                message_metadata=_jsonable(
                    {
                        "agent_dialogue_id": str(dialogue.id),
                        "control_action": action,
                        "control_revision": int(control_revision),
                        "root_control_turn": True,
                        "root_agent_control_relay": True,
                        "agent_run_id": str(root_run_uuid),
                        "source_run_id": str(root_run_uuid),
                        "source_work_item_id": str(root_work_id)
                        if root_work_id
                        else None,
                        "source_human_control_message_id": str(trigger_id),
                        "source_human_control_owner_id": str(owner_uuid),
                        "source_human_control": True,
                        **(
                            dict(recovery_marker)
                            if recovery_marker is not None
                            else {}
                        ),
                    }
                ),
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db_session.add(root_agent_message)
            await db_session.flush()
        else:
            relay_metadata = (
                root_agent_message.message_metadata
                if isinstance(root_agent_message.message_metadata, Mapping)
                else {}
            )
            if (
                root_agent_message.session_id != root_session.id
                or str(root_agent_message.role or "").casefold() != "assistant"
                or str(root_agent_message.sender_type or "").casefold() != "agent"
                or parse_uuid(root_agent_message.sender_id) != root_agent_uuid
                or str(root_agent_message.content or "") != normalized_instruction
                or str(relay_metadata.get("agent_dialogue_id") or "")
                != str(dialogue.id)
                or str(relay_metadata.get("agent_run_id") or "")
                != str(root_run_uuid)
                or str(relay_metadata.get("source_human_control_message_id") or "")
                != str(trigger_id)
                or relay_metadata.get("root_agent_control_relay") is not True
                or (
                    recovery_marker is not None
                    and any(
                        relay_metadata.get(key) != value
                        for key, value in recovery_marker.items()
                    )
                )
            ):
                raise DispatchConflictError(
                    "dialogue control relay binding changed",
                    code="dialogue_control_relay_conflict",
                )
        return {
            "root_run": root_run,
            "root_run_id": str(root_run.get("id")),
            "root_message_id": str(trigger_id),
            "root_agent_message_id": str(root_agent_message_id),
            "root_work_item_id": str(root_work_id) if root_work_id else None,
            "created": bool(created),
            "request_fingerprint": control_digest,
            "source_human_authority_digest": source_human["authority_digest"],
            "source_human_control": control_anchor,
        }

    async def apply_agent_dialogue_control(
        self,
        *,
        dialogue_id: str,
        owner_user_id: str,
        action: str,
        expected_control_revision: int | None = None,
        instruction: str | None = None,
        actor_kind: str | None = None,
        actor_agent_id: str | None = None,
        actor_agent_revision_id: str | None = None,
        actor_run_id: str | None = None,
        actor_work_item_id: str | None = None,
        human_http: bool = False,
    ) -> Dict[str, Any]:
        """Increment a worker dialogue fence with a typed actor proof.

        Human HTTP controls use an explicit ``human_http`` boundary and the
        canonical dialogue owner.  Agent controls must identify the current
        root Agent Run/Work and active lease; no Agent identity is ever
        converted into the Human owner fallback.
        """

        dialogue_uuid = parse_uuid(dialogue_id)
        owner_hint_uuid = parse_uuid(owner_user_id)
        if dialogue_uuid is None or owner_hint_uuid is None:
            raise ValueError("invalid dialogue control identity")
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {"stop", "resume", "rework", "instruction", "cancel"}:
            raise ValueError("unsupported dialogue control action")
        normalized_actor_kind = str(actor_kind or "").strip().lower()
        if normalized_actor_kind not in {"human", "agent"}:
            raise AgentChatAdmissionError(
                "dialogue control actor kind is required",
                code="dialogue_control_actor_kind_required",
            )
        actor_agent_uuid = parse_uuid(actor_agent_id)
        actor_revision_uuid = parse_uuid(actor_agent_revision_id)
        actor_run_uuid = parse_uuid(actor_run_id)
        actor_work_uuid = parse_uuid(actor_work_item_id)
        if normalized_actor_kind == "human":
            if any(
                value is not None
                for value in (
                    actor_agent_uuid,
                    actor_revision_uuid,
                    actor_run_uuid,
                    actor_work_uuid,
                )
            ):
                raise AgentChatAdmissionError(
                    "Human dialogue control cannot carry Agent identity",
                    code="dialogue_control_actor_identity_invalid",
                )
        else:
            if (
                human_http
                or actor_agent_uuid is None
                or actor_revision_uuid is None
                or actor_run_uuid is None
                or actor_work_uuid is None
            ):
                raise AgentChatAdmissionError(
                    "Agent dialogue control requires exact Agent Run/Work identity",
                    code="dialogue_control_agent_identity_required",
                )
        db_session = await self._session()
        try:
            dialogue = (
                await db_session.execute(
                    select(AgentDialogue).where(AgentDialogue.id == dialogue_uuid).with_for_update()
                )
            ).scalars().first()
            if dialogue is None:
                raise AgentChatAdmissionError("dialogue not found", code="dialogue_not_found")
            owner_uuid = await self._require_dialogue_human_owner(db_session, dialogue.owner_user_id)
            if owner_uuid != owner_hint_uuid:
                raise AgentChatAdmissionError("dialogue control owner mismatch", code="dialogue_control_owner_mismatch")
            if normalized_actor_kind == "human" and not human_http:
                # Non-HTTP internal callers must carry a server-bound Human
                # turn. A missing/default ContextVar is never evidence of a
                # Human actor; an Agent turn must use the typed Agent branch.
                try:
                    from .turn_context import get_turn_context

                    human_turn = get_turn_context()
                except Exception as exc:
                    raise AgentChatAdmissionError(
                        "trusted Human turn context is required",
                        code="dialogue_control_human_context_required",
                    ) from exc
                human_role = str(getattr(human_turn, "agent_role", "") or "").strip().casefold()
                if (
                    parse_uuid(getattr(human_turn, "user_id", None)) != owner_uuid
                    or parse_uuid(getattr(human_turn, "session_id", None))
                    != parse_uuid(dialogue.root_session_id)
                    or parse_uuid(getattr(human_turn, "agent_id", None)) is not None
                    or parse_uuid(getattr(human_turn, "agent_revision_id", None)) is not None
                    or getattr(human_turn, "agent_execution_binding", None) is not None
                    or str(getattr(human_turn, "agent_dialogue_role", "") or "").strip()
                    or human_role not in {"", "human", "user"}
                ):
                    raise AgentChatAdmissionError(
                        "Human turn context is not authenticated for this dialogue",
                        code="dialogue_control_human_context_invalid",
                    )
            current = int(dialogue.control_revision or 0)
            if expected_control_revision is not None and int(expected_control_revision) != current:
                raise DispatchConflictError("dialogue control revision changed", code="dialogue_control_revision_changed")
            now = datetime.utcnow()
            actor_source_run = None
            actor_source_work = None
            actor_source_human: dict[str, Any] | None = None
            if normalized_actor_kind == "agent":
                if actor_agent_uuid != parse_uuid(dialogue.root_agent_id):
                    raise AgentChatAdmissionError(
                        "only the canonical root Agent may control this dialogue",
                        code="dialogue_control_agent_binding_invalid",
                    )
                root_session = (
                    await db_session.execute(
                        select(ConversationSession)
                        .where(
                            ConversationSession.id == dialogue.root_session_id,
                            ConversationSession.deleted_at.is_(None),
                        )
                        .with_for_update()
                    )
                ).scalars().first()
                if (
                    root_session is None
                    or parse_uuid(root_session.agent_id) != actor_agent_uuid
                    or root_session.project_id is not None
                ):
                    raise AgentChatAdmissionError(
                        "root Agent session binding is invalid",
                        code="dialogue_control_agent_binding_invalid",
                    )
                actor_source_run = (
                    await db_session.execute(
                        select(AgentRun)
                        .where(AgentRun.id == actor_run_uuid)
                        .with_for_update()
                    )
                ).scalars().first()
                if (
                    actor_source_run is None
                    or actor_source_run.session_id != dialogue.root_session_id
                    or actor_source_run.agent_id != actor_agent_uuid
                    or actor_source_run.agent_revision_id != actor_revision_uuid
                    or actor_source_run.parent_run_id is not None
                    or actor_source_run.status not in RUN_ACTIVE_STATUSES
                    or str(actor_source_run.user_id or "") != str(owner_uuid)
                ):
                    raise AgentChatAdmissionError(
                        "source root Agent Run is not current",
                        code="dialogue_control_source_run_invalid",
                    )
                actor_revision = await db_session.get(AgentRevision, actor_revision_uuid)
                anchor_revision = await db_session.get(
                    AgentRevision,
                    parse_uuid(dialogue.root_agent_revision_id),
                )
                if (
                    actor_revision is None
                    or actor_revision.agent_id != actor_agent_uuid
                    or anchor_revision is None
                    or int(actor_revision.version or 0) < int(anchor_revision.version or 0)
                ):
                    raise AgentChatAdmissionError(
                        "root Agent revision binding is stale",
                        code="dialogue_control_agent_revision_invalid",
                    )
                actor_source_work = (
                    await db_session.execute(
                        select(AgentWorkItem)
                        .where(AgentWorkItem.id == actor_work_uuid)
                        .with_for_update()
                    )
                ).scalars().first()
                active_work_states = {
                    "claimed",
                    "running",
                    "awaiting_approval",
                    "uncertain",
                }
                if (
                    actor_source_work is None
                    or actor_source_run.work_item_id != actor_source_work.id
                    or actor_source_work.active_agent_run_id != actor_source_run.id
                    or actor_source_work.assigned_agent_id != actor_agent_uuid
                    or actor_source_work.conversation_session_id not in (None, dialogue.root_session_id)
                    or str(actor_source_work.state or "") not in active_work_states
                    or not actor_source_work.lease_owner
                    or not actor_source_work.lease_token
                    or actor_source_work.lease_expires_at is None
                    or actor_source_work.lease_expires_at <= now
                ):
                    raise AgentChatAdmissionError(
                        "source root Agent Work lease is not current",
                        code="dialogue_control_source_work_invalid",
                    )
                actor_metadata = (
                    actor_source_run.run_metadata
                    if isinstance(actor_source_run.run_metadata, Mapping)
                    else {}
                )
                actor_dialogue_metadata = actor_metadata.get("agent_dialogue")
                source_human = (
                    actor_dialogue_metadata.get("source_human")
                    if isinstance(actor_dialogue_metadata, Mapping)
                    else None
                )
                source_human = source_human if isinstance(source_human, Mapping) else None
                source_human_message_id = parse_uuid(
                    source_human.get("message_id") if source_human else None
                )
                source_human_run_id = parse_uuid(
                    source_human.get("run_id") if source_human else None
                )
                source_human_work_id = parse_uuid(
                    source_human.get("work_item_id") if source_human else None
                )
                source_human_session_id = parse_uuid(
                    source_human.get("session_id") if source_human else None
                )
                source_human_owner_id = parse_uuid(
                    source_human.get("owner_user_id") if source_human else None
                )
                source_human_message = (
                    await db_session.get(ConversationMessage, source_human_message_id)
                    if source_human_message_id is not None
                    else None
                )
                if (
                    source_human is None
                    or source_human_message_id is None
                    or source_human_run_id is None
                    or source_human_work_id is None
                    or source_human_session_id != dialogue.root_session_id
                    or source_human_owner_id != owner_uuid
                    or source_human_message is None
                    or source_human_message.session_id != dialogue.root_session_id
                    or str(source_human_message.role or "").casefold() != "user"
                    or str(source_human_message.sender_type or "").casefold()
                    not in {"user", "human"}
                    or parse_uuid(source_human_message.sender_id) != owner_uuid
                    or not str(
                        actor_dialogue_metadata.get("source_human_authority_digest")
                        if isinstance(actor_dialogue_metadata, Mapping)
                        else ""
                    ).strip()
                ):
                    raise AgentChatAdmissionError(
                        "Agent control Human ancestry proof is missing or invalid",
                        code="dialogue_control_agent_human_ancestry_invalid",
                    )
                actor_source_human = {
                    **dict(source_human),
                    "message_id": str(source_human_message_id),
                    "run_id": str(source_human_run_id),
                    "work_item_id": str(source_human_work_id),
                    "session_id": str(source_human_session_id),
                    "owner_user_id": str(source_human_owner_id),
                }
            manual_resume_answer = None
            if normalized_actor_kind == "human" and normalized_action == "resume":
                manual_resume_answer = (
                    await self._prepare_manual_resume_answer_recovery(
                        db_session,
                        dialogue=dialogue,
                        expected_control_revision=current,
                    )
                )
            new_revision = current + 1
            active_worker_query = select(AgentRun).where(
                AgentRun.session_id == dialogue.worker_session_id,
                AgentRun.agent_id == dialogue.worker_agent_id,
                AgentRun.status.in_(tuple(RUN_ACTIVE_STATUSES)),
            )
            if normalized_action == "resume":
                active_worker_result = await self._execute_manual_resume_lock_nowait(
                    db_session,
                    active_worker_query,
                )
            else:
                active_worker_result = await db_session.execute(
                    active_worker_query.with_for_update()
                )
            active_worker_runs = active_worker_result.scalars().all()
            if normalized_action in {"resume", "rework"} and active_worker_runs:
                raise DispatchConflictError(
                    "dialogue worker is still active",
                    code="dialogue_worker_active",
                )
            cancelled_run_ids: list[str] = []
            if normalized_action in {"stop", "cancel"}:
                from ..llm.generation_cancellation import request_generation_cancellation

                for worker_run in active_worker_runs:
                    request_generation_cancellation(str(worker_run.id))
                    worker_run.status = "cancelled"
                    worker_run.error = "dialogue_control_stop"
                    worker_run.ended_at = now
                    worker_run.updated_at = now
                    await self._append_event(
                        db_session,
                        worker_run,
                        "run.cancelled",
                        status="cancelled",
                        message="dialogue_control_stop",
                        payload={
                            "dialogue_id": str(dialogue_uuid),
                            "control_revision": new_revision,
                            "actor_kind": normalized_actor_kind,
                            "actor_agent_id": (
                                str(actor_agent_uuid) if actor_agent_uuid else None
                            ),
                            "actor_run_id": str(actor_run_uuid) if actor_run_uuid else None,
                        },
                    )
                    cancelled_run_ids.append(str(worker_run.id))
                    work_id = parse_uuid(worker_run.work_item_id)
                    if work_id is None:
                        continue
                    work = (
                        await db_session.execute(
                            select(AgentWorkItem)
                            .where(AgentWorkItem.id == work_id)
                            .with_for_update()
                        )
                    ).scalars().first()
                    if work is None or str(work.state or "") in {
                        "succeeded",
                        "failed",
                        "cancelled",
                        "dead_letter",
                    }:
                        continue
                    previous_state = str(work.state or "")
                    work.state = "cancelled"
                    work.outcome_classification = "cancelled"
                    work.safe_error_code = "dialogue_control_stop"
                    work.escalation_reason = "dialogue control stop"
                    work.lease_owner = None
                    work.lease_token = None
                    work.lease_expires_at = None
                    work.cancelled_at = now
                    work.updated_at = now
                    work.active_agent_run_id = None
                    work_sequence = int(
                        await db_session.scalar(
                            select(func.max(AgentWorkEvent.sequence)).where(
                                AgentWorkEvent.work_item_id == work.id
                            )
                        )
                        or 0
                    ) + 1
                    db_session.add(
                        AgentWorkEvent(
                            id=uuid.uuid4(),
                            work_item_id=work.id,
                            sequence=work_sequence,
                            event_type="work.cancelled",
                            from_state=previous_state,
                            to_state="cancelled",
                            actor_kind=normalized_actor_kind,
                            actor_id=(
                                str(actor_agent_uuid)
                                if normalized_actor_kind == "agent"
                                else str(owner_uuid)
                            ),
                            actor_user_id=(
                                owner_uuid
                                if normalized_actor_kind == "human"
                                else None
                            ),
                            actor_agent_id=(
                                actor_agent_uuid
                                if normalized_actor_kind == "agent"
                                else None
                            ),
                            agent_run_id=worker_run.id,
                            safe_error_code="dialogue_control_stop",
                            message="dialogue control stop",
                            payload_json={
                                "dialogue_id": str(dialogue_uuid),
                                "control_revision": new_revision,
                                "actor_kind": normalized_actor_kind,
                                "actor_agent_id": (
                                    str(actor_agent_uuid) if actor_agent_uuid else None
                                ),
                                "actor_run_id": str(actor_run_uuid) if actor_run_uuid else None,
                            },
                            created_at=now,
                        )
                    )
            pending_input_query = select(AgentDialogueInput).where(
                AgentDialogueInput.dialogue_id == dialogue_uuid,
                AgentDialogueInput.state == "pending",
            )
            if normalized_action == "resume":
                pending_input_result = await self._execute_manual_resume_lock_nowait(
                    db_session,
                    pending_input_query,
                )
            else:
                pending_input_result = await db_session.execute(
                    pending_input_query.with_for_update()
                )
            pending_rows = pending_input_result.scalars().all()
            invalidated: list[str] = []
            for row in pending_rows:
                row.state = "cancelled"
                row.updated_at = now
                invalidated.append(str(row.id))
            dialogue.control_revision = new_revision
            root_control_turn: Dict[str, Any] | None = None
            agent_control_message_id: uuid.UUID | None = None
            control_instruction_digest = self._dialogue_digest(instruction or "")
            control_digest = self._dialogue_digest(
                {
                    "dialogue_id": str(dialogue_uuid),
                    "action": normalized_action,
                    "control_revision": new_revision,
                    "instruction_digest": control_instruction_digest,
                    "actor_kind": normalized_actor_kind,
                    "actor_agent_id": str(actor_agent_uuid) if actor_agent_uuid else None,
                    "actor_agent_revision_id": (
                        str(actor_revision_uuid) if actor_revision_uuid else None
                    ),
                    "actor_run_id": str(actor_run_uuid) if actor_run_uuid else None,
                    "actor_work_item_id": str(actor_work_uuid) if actor_work_uuid else None,
                }
            )
            if (
                normalized_actor_kind == "human"
                and normalized_action in {"resume", "rework", "instruction"}
            ):
                root_control_turn = await self._create_dialogue_control_root_turn(
                    db_session,
                    dialogue=dialogue,
                    owner_uuid=owner_uuid,
                    action=normalized_action,
                    control_revision=new_revision,
                    instruction=instruction,
                    answer_recovery=manual_resume_answer,
                )
            elif (
                normalized_actor_kind == "agent"
                and normalized_action not in {"stop", "cancel"}
            ):
                # Agent controls must never reuse the Root Run trigger.  That
                # trigger is normally the original Human user Message, and
                # binding it as a Worker Delivery source makes the Worker
                # reject an otherwise valid control as a forged sender.
                relay_message_id = uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"aoitalk:agent-dialogue-agent-control-relay:{dialogue.id}:{new_revision}:{normalized_action}:{control_digest}",
                )
                relay_body = str(instruction or normalized_action).strip()[:30_000]
                if not relay_body:
                    relay_body = normalized_action
                relay_message = await db_session.get(
                    ConversationMessage,
                    relay_message_id,
                )
                relay_metadata = {
                    "agent_dialogue_id": str(dialogue.id),
                    "control_action": normalized_action,
                    "control_revision": int(new_revision),
                    "agent_dialogue_control_relay": True,
                    "agent_control_actor": True,
                    "agent_run_id": str(actor_source_run.id),
                    "source_run_id": str(actor_source_run.id),
                    "source_work_item_id": str(actor_source_work.id),
                    "agent_control_source_human": actor_source_human,
                    "agent_control_source_human_authority_digest": (
                        actor_dialogue_metadata.get("source_human_authority_digest")
                        if isinstance(actor_dialogue_metadata, Mapping)
                        else None
                    ),
                }
                if relay_message is None:
                    relay_message = ConversationMessage(
                        id=relay_message_id,
                        session_id=dialogue.root_session_id,
                        role="assistant",
                        content=relay_body,
                        client_message_id=(
                            f"dialogue-agent-control-relay:{dialogue.id}:"
                            f"{new_revision}:{normalized_action}"
                        ),
                        sender_type="agent",
                        sender_id=str(actor_agent_uuid),
                        sender_display_name="Agent",
                        message_metadata=_jsonable(relay_metadata),
                        created_at=now,
                        updated_at=now,
                    )
                    db_session.add(relay_message)
                    await db_session.flush()
                else:
                    existing_metadata = (
                        relay_message.message_metadata
                        if isinstance(relay_message.message_metadata, Mapping)
                        else {}
                    )
                    if (
                        relay_message.session_id != dialogue.root_session_id
                        or str(relay_message.role or "").casefold() != "assistant"
                        or str(relay_message.sender_type or "").casefold() != "agent"
                        or parse_uuid(relay_message.sender_id) != actor_agent_uuid
                        or str(relay_message.content or "") != relay_body
                        or existing_metadata != relay_metadata
                    ):
                        raise DispatchConflictError(
                            "dialogue Agent control relay binding changed",
                            code="dialogue_control_relay_conflict",
                        )
                agent_control_message_id = relay_message_id
            if normalized_action in {"stop", "cancel"}:
                dialogue.state = "cancelled"
            elif normalized_action in {"resume", "rework"}:
                dialogue.state = "queued"
            dialogue.updated_at = now
            control_key = f"dialogue-control:{dialogue_uuid}:{new_revision}:{normalized_action}"
            existing = (
                await db_session.execute(
                    select(AgentDialogueDelivery).where(
                        AgentDialogueDelivery.dialogue_id == dialogue_uuid,
                        AgentDialogueDelivery.idempotency_key == control_key,
                    )
                )
            ).scalars().first()
            if existing is None:
                delivery_sequence = await self._next_agent_dialogue_delivery_sequence(
                    db_session,
                    dialogue_uuid,
                )
                control_delivery = AgentDialogueDelivery(
                        id=uuid.uuid4(),
                        dialogue_id=dialogue_uuid,
                        source_session_id=dialogue.root_session_id,
                        source_agent_id=dialogue.root_agent_id,
                        source_agent_revision_id=(
                            actor_revision_uuid
                            if normalized_actor_kind == "agent"
                            else dialogue.root_agent_revision_id
                        ),
                        receiver_session_id=dialogue.worker_session_id,
                        receiver_agent_id=dialogue.worker_agent_id,
                        receiver_agent_revision_id=dialogue.worker_agent_revision_id,
                        source_run_id=(
                            parse_uuid(root_control_turn["root_run_id"])
                            if root_control_turn is not None
                            else actor_run_uuid
                            if normalized_actor_kind == "agent"
                            and normalized_action not in {"stop", "cancel"}
                            else None
                        ),
                        source_work_item_id=(
                            parse_uuid(
                                (root_control_turn or {}).get("root_work_item_id")
                            )
                            if root_control_turn is not None
                            else actor_work_uuid
                            if normalized_actor_kind == "agent"
                            and normalized_action not in {"stop", "cancel"}
                            else None
                        ),
                        source_message_id=(
                            parse_uuid(
                                root_control_turn.get("root_agent_message_id")
                                or root_control_turn["root_message_id"]
                            )
                            if root_control_turn is not None
                            else agent_control_message_id
                            if normalized_actor_kind == "agent"
                            and normalized_action not in {"stop", "cancel"}
                            else None
                        ),
                        kind="control",
                        state="pending",
                        sequence=delivery_sequence,
                        expected_control_revision=new_revision,
                        idempotency_key=control_key,
                        request_digest=control_digest,
                        created_at=now,
                        updated_at=now,
                )
                db_session.add(control_delivery)
                await db_session.flush()
                if (
                    control_delivery.source_run_id is not None
                    and control_delivery.source_work_item_id is not None
                    and control_delivery.source_message_id is not None
                ):
                    # Control deliveries with a Root Run/Work/Agent relay use
                    # the same finite provider-free transport Work lane as
                    # request/response deliveries. Stop/cancel controls have
                    # no source execution and intentionally remain without a
                    # handoff Work item.
                    from .agent_dialogue_transport import AgentDialogueTransport

                    control_transport = AgentDialogueTransport(
                        self._get_db_manager(),
                        dispatch_bridge=self,
                        config=self.config,
                        worker_id="dialogue-control",
                    )
                    await control_transport._transport_work(
                        db_session,
                        dialogue,
                        control_delivery,
                        now,
                    )
            await db_session.commit()
            return {
                "dialogue_id": str(dialogue_uuid),
                "action": normalized_action,
                "control_revision": new_revision,
                "invalidated_input_ids": invalidated,
                "cancelled_run_ids": cancelled_run_ids,
                "root_directive_required": (
                    normalized_actor_kind == "human"
                    and normalized_action in {"resume", "rework", "instruction"}
                ),
                "actor_kind": normalized_actor_kind,
                "actor_agent_id": str(actor_agent_uuid) if actor_agent_uuid else None,
                "actor_agent_revision_id": (
                    str(actor_revision_uuid) if actor_revision_uuid else None
                ),
                "actor_run_id": str(actor_run_uuid) if actor_run_uuid else None,
                "actor_work_item_id": str(actor_work_uuid) if actor_work_uuid else None,
                "instruction_prefill": str(instruction or "")[:30_000] or None,
                "receipt_idempotency_key": control_key,
                "root_control_run_id": (
                    root_control_turn["root_run_id"]
                    if root_control_turn is not None
                    else None
                ),
                "root_control_message_id": (
                    root_control_turn["root_message_id"]
                    if root_control_turn is not None
                    else None
                ),
                "agent_control_message_id": (
                    str(agent_control_message_id)
                    if agent_control_message_id is not None
                    else None
                ),
                "root_control_run_created": (
                    bool(root_control_turn and root_control_turn.get("created"))
                ),
            }
        except Exception:
            await db_session.rollback()
            raise
        finally:
            await db_session.close()

    async def dispatch_dialogue_human_input(
        self,
        *,
        dialogue_id: str,
        input_id: str,
        answer_message_id: str,
        owner_user_id: str,
        expected_control_revision: int,
        expected_input_revision: int,
        client_message_id: str,
        recovery_from_run_id: str | None = None,
    ) -> Dict[str, Any]:
        """Create the root Agent's finite answer turn from a Human message.

        The Human-authored root message remains ``sender_type=user`` and is
        reused as the persisted trigger.  This transaction creates the root
        Agent Run/interactive Work/ordinary outbox and never rewrites the
        Human into an Agent sender.  The root Agent's next response can then
        issue a typed Delivery to the worker through the normal transport.
        """

        dialogue_uuid = parse_uuid(dialogue_id)
        input_uuid = parse_uuid(input_id)
        answer_uuid = parse_uuid(answer_message_id)
        owner_uuid = parse_uuid(owner_user_id)
        recovery_from_uuid = parse_uuid(recovery_from_run_id)
        try:
            input_revision = int(expected_input_revision)
            control_revision = int(expected_control_revision)
        except (TypeError, ValueError) as exc:
            raise AgentChatAdmissionError(
                "dialogue answer revision is invalid",
                code="dialogue_answer_revision_invalid",
            ) from exc
        if (
            dialogue_uuid is None
            or input_uuid is None
            or answer_uuid is None
            or owner_uuid is None
            or input_revision < 1
            or control_revision < 0
            or not str(client_message_id or "").strip()
            or (recovery_from_run_id not in (None, "") and recovery_from_uuid is None)
        ):
            raise AgentChatAdmissionError(
                "dialogue Human answer identity is incomplete",
                code="dialogue_answer_identity_invalid",
            )
        db_session = await self._session()
        owns_session = True
        try:
            input_row = (
                await db_session.execute(
                    select(AgentDialogueInput)
                    .where(AgentDialogueInput.id == input_uuid)
                    .with_for_update()
                )
            ).scalars().first()
            dialogue = (
                await db_session.execute(
                    select(AgentDialogue)
                    .where(AgentDialogue.id == dialogue_uuid)
                    .with_for_update()
                )
            ).scalars().first()
            if input_row is None or dialogue is None or input_row.dialogue_id != dialogue_uuid:
                raise AgentChatAdmissionError(
                    "dialogue Human answer source is unavailable",
                    code="dialogue_answer_source_invalid",
                )
            if int(input_row.revision or 0) != input_revision:
                raise DispatchConflictError(
                    "dialogue input revision is stale",
                    code="dialogue_input_revision_stale",
                )
            if int(input_row.expected_control_revision or 0) != control_revision or int(dialogue.control_revision or 0) != control_revision:
                raise DispatchConflictError(
                    "dialogue input control revision is stale",
                    code="dialogue_input_control_revision_stale",
                )
            if str(input_row.state or "") != "answered" or input_row.answer_message_id != answer_uuid:
                raise AgentChatAdmissionError(
                    "dialogue Human answer has not been prepared",
                    code="dialogue_answer_not_prepared",
                )
            owner_uuid = await self._require_dialogue_human_owner(
                db_session,
                dialogue.owner_user_id,
            )
            if owner_uuid != parse_uuid(owner_user_id):
                raise AgentChatAdmissionError(
                    "dialogue Human answer owner mismatch",
                    code="dialogue_answer_owner_mismatch",
                )
            root_session = (
                await db_session.execute(
                    select(ConversationSession)
                    .where(
                        ConversationSession.id == dialogue.root_session_id,
                        ConversationSession.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
            ).scalars().first()
            answer_message = await db_session.get(ConversationMessage, answer_uuid)
            if root_session is None or answer_message is None:
                raise AgentChatAdmissionError(
                    "dialogue Human answer message is unavailable",
                    code="dialogue_answer_message_invalid",
                )
            if (
                answer_message.session_id != root_session.id
                or str(answer_message.role or "").casefold() != "user"
                or str(answer_message.sender_type or "").casefold() not in {"user", "human"}
                or parse_uuid(answer_message.sender_id) != owner_uuid
            ):
                raise AgentChatAdmissionError(
                    "dialogue Human answer message sender is invalid",
                    code="dialogue_answer_message_invalid",
                )
            continuation_source_run = await db_session.get(
                AgentRun,
                input_row.source_run_id,
            )
            continuation_source_work = (
                await db_session.get(AgentWorkItem, input_row.source_work_item_id)
                if input_row.source_work_item_id is not None
                else None
            )
            continuation_source_message = (
                await db_session.get(ConversationMessage, input_row.source_message_id)
                if input_row.source_message_id is not None
                else None
            )
            if (
                continuation_source_run is None
                or continuation_source_work is None
                or continuation_source_message is None
                or continuation_source_run.session_id != dialogue.worker_session_id
                or continuation_source_run.agent_id != dialogue.worker_agent_id
                or continuation_source_run.agent_revision_id
                != dialogue.worker_agent_revision_id
                or parse_uuid(continuation_source_run.user_id)
                != owner_uuid
                or continuation_source_run.work_item_id != continuation_source_work.id
                or continuation_source_work.conversation_session_id
                != dialogue.worker_session_id
                or continuation_source_work.assigned_agent_id
                != dialogue.worker_agent_id
                or continuation_source_work.agent_revision_id
                != dialogue.worker_agent_revision_id
                or str(continuation_source_work.source_type or "") != "chat_turn"
                or str(continuation_source_work.execution_adapter or "")
                != "interactive_chat"
                or str(continuation_source_work.source_id or "")
                != str(continuation_source_run.id)
                or str(continuation_source_work.source_revision or "")
                != str(dialogue.worker_agent_revision_id)
                or continuation_source_message.session_id
                != dialogue.worker_session_id
                or str(continuation_source_message.role or "").casefold()
                != "assistant"
                or str(continuation_source_message.sender_type or "").casefold()
                != "agent"
                or parse_uuid(continuation_source_message.sender_id)
                != dialogue.worker_agent_id
                or not isinstance(
                    continuation_source_message.message_metadata,
                    Mapping,
                )
                or parse_uuid(
                    continuation_source_message.message_metadata.get(
                        "agent_dialogue_id"
                    )
                )
                != dialogue.id
                or parse_uuid(
                    continuation_source_message.message_metadata.get(
                        "agent_dialogue_input_id"
                    )
                )
                != input_row.id
            ):
                raise AgentChatAdmissionError(
                    "dialogue continuation source binding is invalid",
                    code="dialogue_continuation_target_invalid",
                )
            continuation_target = self._dialogue_continuation_target(
                dialogue=dialogue,
                input_row=input_row,
                source_run=continuation_source_run,
                source_work=continuation_source_work,
                source_message=continuation_source_message,
                answer_message_id=answer_uuid,
                root_run_id="pending-root-run",
                root_work_item_id="pending-root-work",
            )
            root_agent_id = parse_uuid(root_session.agent_id) or parse_uuid(dialogue.root_agent_id)
            if root_agent_id is None or root_agent_id != parse_uuid(dialogue.root_agent_id):
                raise AgentChatAdmissionError(
                    "dialogue root Agent binding changed",
                    code="dialogue_root_agent_binding_changed",
                )
            body = str(answer_message.content or "")
            human_run_uuid = parse_uuid(getattr(dialogue, "human_run_id", None))
            human_run = (
                await db_session.get(AgentRun, human_run_uuid)
                if human_run_uuid is not None
                else None
            )
            if human_run is None and getattr(dialogue, "human_message_id", None) is not None:
                human_message_uuid = parse_uuid(dialogue.human_message_id)
                if human_message_uuid is not None:
                    human_run = (
                        await db_session.execute(
                            select(AgentRun)
                            .where(AgentRun.trigger_message_id == human_message_uuid)
                            .order_by(AgentRun.created_at.asc())
                            .limit(1)
                        )
                    ).scalars().first()
            if human_run is None:
                raise AgentChatAdmissionError(
                    "dialogue Human anchor is unavailable",
                    code="dialogue_source_human_anchor_missing",
                )
            human_work_uuid = parse_uuid(getattr(dialogue, "human_work_item_id", None))
            human_work = (
                await db_session.get(AgentWorkItem, human_work_uuid)
                if human_work_uuid is not None
                else None
            )
            original_human_anchor = await self._build_dialogue_human_anchor(
                db_session,
                source_run=human_run,
                source_work=human_work,
                include_body=False,
                dialogue=dialogue,
            )
            answer_human_refs = {
                "message_id": str(answer_uuid),
                # Keep the original Human Run/Work trace attached to the
                # answer message; the answer itself is the current source
                # body, while the original job remains the authority scope.
                "run_id": original_human_anchor["refs"]["run_id"],
                "work_item_id": original_human_anchor["refs"].get("work_item_id"),
                "owner_user_id": str(owner_uuid),
                "session_id": str(root_session.id),
                "binding_hash": original_human_anchor["refs"].get("binding_hash"),
                "manifest_hash": original_human_anchor["refs"].get("manifest_hash"),
                "command_capabilities": list(
                    original_human_anchor["refs"].get("command_capabilities") or []
                ),
            }
            answer_human_anchor = {
                **answer_human_refs,
                "body_digest": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "authority_digest": self._dialogue_digest(answer_human_refs),
            }
            body_digest = answer_human_anchor["body_digest"]
            # This is the actual persisted Message body digest.  The
            # Input.answer_digest above is the canonical wire digest and
            # intentionally has different semantics.
            request_digest = self._dialogue_human_answer_request_fingerprint(
                dialogue_id=dialogue_uuid,
                input_id=input_uuid,
                input_revision=input_revision,
                control_revision=control_revision,
                body_digest=body_digest,
                recovery_from_run_id=recovery_from_uuid,
            )
            dispatch_metadata = {
                "agent_dialogue": {
                    "dialogue_id": str(dialogue_uuid),
                    "role": "root",
                    "input_id": str(input_uuid),
                    "input_revision": input_revision,
                    "control_revision": control_revision,
                    "source_human_message_id": str(answer_uuid),
                    "source_human_owner_id": str(owner_uuid),
                    "source_human_answered": True,
                    "source_human": {
                        **dict(original_human_anchor["refs"]),
                        "body_digest": original_human_anchor["body_digest"],
                    },
                    "source_human_authority_digest": original_human_anchor[
                        "authority_digest"
                    ],
                    "source_human_answer": answer_human_anchor,
                    "authority_digest": original_human_anchor["authority_digest"],
                    "continuation_target": continuation_target,
                }
            }
            # The browser/client nonce is an audit alias only.  A retried
            # answer can arrive with a different nonce after reconnecting;
            # the persisted answer identity is the durable idempotency key
            # for the finite root turn.
            canonical_client_message_id = (
                f"dialogue-human-answer:{input_uuid}:{input_revision}:{answer_uuid}"
            )
            if recovery_from_uuid is not None:
                # Keep the old failed Root Run immutable and isolate the
                # retry under one deterministic fresh idempotency key.
                canonical_client_message_id = (
                    f"{canonical_client_message_id}:recovery:{recovery_from_uuid}"
                )
                from .agent_dialogue_transport import (
                    prove_failed_answer_root_recovery,
                )

                proven_old_run_id = await prove_failed_answer_root_recovery(
                    db_session,
                    dialogue=dialogue,
                    input_row=input_row,
                    answer_message=answer_message,
                    canonical_client_id=(
                        f"dialogue-human-answer:{input_uuid}:"
                        f"{input_revision}:{answer_uuid}"
                    ),
                )
                if proven_old_run_id != str(recovery_from_uuid):
                    raise AgentChatAdmissionError(
                        "dialogue answer recovery proof changed",
                        code="dialogue_answer_recovery_proof_invalid",
                    )
            dispatch_metadata["agent_dialogue"]["client_message_id_alias"] = str(
                client_message_id
            )[:512]
            if recovery_from_uuid is not None:
                dispatch_metadata["agent_dialogue"]["recovery_from_run_id"] = str(
                    recovery_from_uuid
                )
                dispatch_metadata["agent_dialogue"]["recovery_kind"] = (
                    "failed_pre_provider_answer_root"
                )
            dispatch_payload = {
                "_dialogue_human_input": True,
                "agent_dialogue": _jsonable(dispatch_metadata["agent_dialogue"]),
                "persisted_user_message_id": str(answer_uuid),
                "skip_user_persistence": True,
            }
            await self._supersede_unstarted_dialogue_question_root(
                db_session,
                dialogue=dialogue,
                input_row=input_row,
                answer_message_id=answer_uuid,
            )
            root_run, trigger_id, created = await self.create_or_get_dispatch_turn(
                session_id=str(root_session.id),
                client_message_id=canonical_client_message_id,
                content=body,
                message_metadata={
                    "agent_dialogue_id": str(dialogue_uuid),
                    "agent_dialogue_input_id": str(input_uuid),
                    "input_revision": input_revision,
                    "control_revision": control_revision,
                    "human_answer_source": True,
                    **(
                        {"recovery_from_run_id": str(recovery_from_uuid)}
                        if recovery_from_uuid is not None
                        else {}
                    ),
                },
                sender_type="user",
                sender_id=str(owner_uuid),
                sender_display_name="User",
                edit_message_id=None,
                outbox_payload=dispatch_payload,
                request_fingerprint=request_digest,
                persisted_user_message_id=str(answer_uuid),
                user_id=str(owner_uuid),
                project_id=None,
                objective=body,
                metadata=dispatch_metadata,
                agent_id=str(root_agent_id),
                expected_agent_id=str(root_agent_id),
                expected_agent_provided=True,
                context_project_hint_id=None,
                session=db_session,
            )
            root_run_uuid = parse_uuid(root_run.get("id")) if isinstance(root_run, Mapping) else None
            root_work_uuid = (
                parse_uuid(root_run.get("work_item_id"))
                if isinstance(root_run, Mapping)
                else None
            )
            persisted_root = (
                await db_session.get(AgentRun, root_run_uuid)
                if root_run_uuid is not None
                else None
            )
            persisted_root_work = (
                await db_session.get(AgentWorkItem, root_work_uuid)
                if root_work_uuid is not None
                else None
            )
            final_continuation_target = self._dialogue_continuation_target(
                dialogue=dialogue,
                input_row=input_row,
                source_run=continuation_source_run,
                source_work=continuation_source_work,
                source_message=continuation_source_message,
                answer_message_id=answer_uuid,
                root_run_id=root_run_uuid,
                root_work_item_id=(
                    persisted_root_work.id if persisted_root_work is not None else None
                ),
            )
            stored_root_marker = (
                persisted_root.run_metadata.get("agent_dialogue")
                if persisted_root is not None
                and isinstance(persisted_root.run_metadata, Mapping)
                else None
            )
            stored_target = (
                stored_root_marker.get("continuation_target")
                if isinstance(stored_root_marker, Mapping)
                else None
            )
            if created or stored_target in (None, {}):
                if not created:
                    raise AgentChatAdmissionError(
                        "dialogue continuation target is missing",
                        code="dialogue_continuation_target_invalid",
                    )
                if persisted_root is None or persisted_root_work is None:
                    raise AgentChatAdmissionError(
                        "dialogue continuation Root binding is unavailable",
                        code="dialogue_continuation_target_invalid",
                    )
                root_metadata = dict(persisted_root.run_metadata or {})
                root_marker = dict(root_metadata.get("agent_dialogue") or {})
                root_marker["continuation_target"] = final_continuation_target
                root_metadata["agent_dialogue"] = root_marker
                persisted_root.run_metadata = root_metadata
                persisted_outbox = await db_session.get(
                    ConversationDispatchOutbox,
                    root_run_uuid,
                )
                if persisted_outbox is None or not isinstance(
                    persisted_outbox.payload, Mapping
                ):
                    raise AgentChatAdmissionError(
                        "dialogue continuation outbox is unavailable",
                        code="dialogue_continuation_target_invalid",
                    )
                outbox_payload = dict(persisted_outbox.payload)
                outbox_marker = dict(outbox_payload.get("agent_dialogue") or {})
                outbox_marker["continuation_target"] = final_continuation_target
                outbox_payload["agent_dialogue"] = outbox_marker
                persisted_outbox.payload = outbox_payload
            elif stored_target != final_continuation_target:
                raise AgentChatAdmissionError(
                    "dialogue continuation target changed",
                    code="dialogue_continuation_target_invalid",
                )
            root_run_is_queueable = (
                created
                and isinstance(root_run, Mapping)
                and root_run.get("status") in RUN_ACTIVE_STATUSES
            )
            if root_run_is_queueable:
                now = datetime.utcnow()
                await db_session.execute(
                    update(ConversationSession)
                    .where(ConversationSession.id == root_session.id)
                    .values(
                        message_count=ConversationSession.message_count + 1,
                        last_activity=_monotonic_activity(now),
                        development_status="working",
                    )
                )
                dialogue.state = "running"
                dialogue.updated_at = now
            await db_session.commit()
            return {
                "success": True,
                "dialogue_id": str(dialogue_uuid),
                "input_id": str(input_uuid),
                "input_revision": input_revision,
                "control_revision": control_revision,
                "root_run": root_run,
                "root_run_id": str(root_run.get("id")),
                "root_message_id": str(trigger_id),
                "created": bool(created),
                "recovery_from_run_id": (
                    str(recovery_from_uuid) if recovery_from_uuid is not None else None
                ),
            }
        except Exception:
            if owns_session:
                await db_session.rollback()
            raise
        finally:
            if owns_session:
                await db_session.close()

    async def _apply_agent_dialogue_control_for_session(
        self,
        *,
        session_id: str,
        owner_user_id: str,
        action: str = "stop",
    ) -> list[Dict[str, Any]]:
        """Apply direct human child-session control without touching root jobs."""

        session_uuid = parse_uuid(session_id)
        if session_uuid is None:
            return []
        db_session = await self._session()
        try:
            rows = (
                await db_session.execute(
                    select(AgentDialogue.id).where(
                        AgentDialogue.worker_session_id == session_uuid,
                        AgentDialogue.owner_user_id == parse_uuid(owner_user_id),
                    )
                )
            ).scalars().all()
        finally:
            await db_session.close()
        receipts: list[Dict[str, Any]] = []
        for dialogue_uuid in rows:
            receipts.append(
                await self.apply_agent_dialogue_control(
                    dialogue_id=str(dialogue_uuid),
                    owner_user_id=owner_user_id,
                    action=action,
                    actor_kind="human",
                    human_http=True,
                )
            )
        return receipts

    async def create_run(
        self,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        client_message_id: str | None = None,
        request_fingerprint: str | None = None,
        project_id: str | None = None,
        context_project_hint_id: str | None = None,
        app_id: str | None = None,
        app_target_id: str | None = None,
        base_revision: str | None = None,
        result_revision: str | None = None,
        trigger_message_id: str | None = None,
        objective: str = "",
        run_type: str = "chat_turn",
        generation_profile: str | None = None,
        metadata: Dict[str, Any] | None = None,
        title: str | None = None,
        parent_run_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        agent_id: str | None = None,
        agent_revision_id: str | None = None,
        expected_agent_id: Any = _EXPECTED_AGENT_OMITTED,
        expected_agent_provided: bool | None = None,
        task_id: str | None = None,
        work_item_id: str | None = None,
        work_item_attempt: int | None = None,
        acting_subagent_id: str | None = None,
        previous_attempt_run_id: str | None = None,
        execution_manifest: Dict[str, Any] | None = None,
        execution_manifest_hash: str | None = None,
    ) -> Dict[str, Any]:
        session = await self._session()
        try:
            now = datetime.utcnow()
            context_hint_uuid = parse_uuid(context_project_hint_id)
            if context_project_hint_id not in (None, "") and context_hint_uuid is None:
                raise ValueError("invalid context_project_hint_id")
            locked_conversation = None
            parent_uuid = parse_uuid(parent_run_id)
            root_uuid = None
            if parent_uuid:
                parent = await session.get(AgentRun, parent_uuid)
                if parent:
                    root_uuid = parent.root_run_id or parent.id
                    # 子runの所有権は呼び出し側の値に関係なく親へ固定する。
                    # 空値や別scopeの明示指定を許すと認可境界を越えられる。
                    session_id = (
                        str(parent.session_id) if parent.session_id else None
                    )
                    user_id = str(parent.user_id) if parent.user_id else None
                    project_id = (
                        str(parent.project_id) if parent.project_id else None
                    )
                    app_id = str(parent.app_id) if parent.app_id else None
                    app_target_id = (
                        str(parent.app_target_id) if parent.app_target_id else None
                    )
                    base_revision = parent.base_revision
                    result_revision = parent.result_revision
                    # A child run cannot manufacture a different autonomous
                    # identity or revision from request payload.  Preserve
                    # the parent's typed identity when one exists.
                    agent_id = str(parent.agent_id) if parent.agent_id else None
                    agent_revision_id = (
                        str(parent.agent_revision_id)
                        if parent.agent_revision_id
                        else None
                    )
                    task_id = str(parent.task_id) if parent.task_id else None
                    work_item_id = str(parent.work_item_id) if parent.work_item_id else None
                    acting_subagent_id = parent.acting_subagent_id
                    previous_attempt_run_id = (
                        str(parent.previous_attempt_run_id)
                        if parent.previous_attempt_run_id
                        else previous_attempt_run_id
                    )
                    execution_manifest = (
                        dict(parent.resolved_execution_manifest)
                        if isinstance(parent.resolved_execution_manifest, dict)
                        else execution_manifest
                    )
                    execution_manifest_hash = parent.execution_manifest_hash or execution_manifest_hash

            if str(run_type or "chat_turn").strip() == "chat_turn" and session_id:
                session_uuid_for_lock = parse_uuid(session_id)
                if session_uuid_for_lock is None:
                    # Lightweight/legacy unauthenticated Chat adapters may
                    # carry symbolic session labels.  Preserve their
                    # human-only bookkeeping path as an orphan-free Run with
                    # a NULL canonical session; any typed Agent or expected
                    # binding still requires a real UUID and fails closed.
                    if agent_id is not None or expected_agent_provided:
                        raise ValueError("invalid session_id")
                else:
                    locked_conversation = (
                        await session.execute(
                            select(ConversationSession)
                            .where(
                                ConversationSession.id == session_uuid_for_lock,
                                ConversationSession.deleted_at.is_(None),
                            )
                            .with_for_update()
                        )
                    ).scalars().first()
                    if locked_conversation is None:
                        raise ValueError("Session not found or deleted")
                    if context_hint_uuid is not None:
                        if locked_conversation.project_id is not None or project_id is not None:
                            raise AgentChatAdmissionError(
                                "AoiDots root session must remain projectless",
                                code="dialogue_root_project_forbidden",
                            )
                    _check_expected_agent_binding(
                        locked_conversation,
                        expected_agent_id=expected_agent_id,
                        expected_agent_provided=expected_agent_provided,
                    )

            agent_uuid = parse_uuid(agent_id)
            revision_uuid = parse_uuid(agent_revision_id)
            task_uuid = parse_uuid(task_id)
            work_item_uuid = parse_uuid(work_item_id)
            previous_attempt_uuid = parse_uuid(previous_attempt_run_id)
            if work_item_attempt is not None:
                try:
                    work_item_attempt = int(work_item_attempt)
                except (TypeError, ValueError) as exc:
                    raise ValueError("invalid work_item_attempt") from exc
                if work_item_attempt < 1:
                    raise ValueError("work_item_attempt must be positive")
            if task_id and task_uuid is None:
                raise ValueError("invalid task_id")
            if work_item_id and work_item_uuid is None:
                raise ValueError("invalid work_item_id")
            if previous_attempt_run_id and previous_attempt_uuid is None:
                raise ValueError("invalid previous_attempt_run_id")
            if agent_revision_id and revision_uuid is None:
                raise ValueError("invalid agent_revision_id")
            if agent_id and agent_uuid is None:
                raise ValueError("invalid agent_id")
            if revision_uuid is not None and agent_uuid is None:
                raise ValueError("agent_revision_id requires agent_id")
            if agent_uuid is not None and revision_uuid is None:
                raise ValueError("typed Agent runs require an exact AgentRevision")
            existing_attempt = None
            agent_row = None
            # A legacy ``user_id`` is a human-only column.  Reject a UUID that
            # resolves to an Agent even when the caller omitted the typed
            # ``agent_id`` field; otherwise a client can silently impersonate
            # an Agent by writing its ID through the old column.
            user_uuid_candidate = parse_uuid(user_id)
            if user_uuid_candidate is not None:
                user_agent_row = await _lookup_agent_for_legacy_user(
                    session, user_uuid_candidate
                )
                if user_agent_row is not None:
                    raise ValueError("Agent IDs must not be stored in user_id")
            if agent_uuid is not None:
                agent_row = await session.get(Agent, agent_uuid)
                if agent_row is None or str(agent_row.state or "") != "active":
                    raise ValueError("agent is not active")
                if user_id and (
                    str(user_id).strip() == str(agent_uuid)
                    or parse_uuid(user_id) == agent_uuid
                ):
                    raise ValueError("Agent IDs must not be stored in user_id")
            revision_row = None
            if revision_uuid is not None:
                revision_row = await session.get(AgentRevision, revision_uuid)
                if revision_row is None or revision_row.agent_id != agent_uuid:
                    raise ValueError("agent revision is not bound to agent")
            _validate_typed_agent_actor(
                user_id=user_id,
                agent_id=agent_uuid,
                agent_revision=revision_row if revision_uuid is not None else None,
                acting_subagent_id=acting_subagent_id,
                agent_row=agent_row,
                config=self.config,
            )
            if previous_attempt_uuid is not None:
                previous_row = await session.get(AgentRun, previous_attempt_uuid)
                if previous_row is None:
                    raise ValueError("previous attempt run not found")
                if agent_uuid is not None and previous_row.agent_id != agent_uuid:
                    raise ValueError("previous attempt run belongs to another agent")
            if task_uuid is not None:
                task_row = await session.get(Task, task_uuid)
                if task_row is None:
                    raise ValueError("task not found")
                requested_project_uuid = parse_uuid(project_id)
                if requested_project_uuid is not None and task_row.project_id != requested_project_uuid:
                    raise ValueError("task does not belong to project")
            if work_item_uuid is not None:
                work_item_row = await session.get(AgentWorkItem, work_item_uuid)
                if work_item_row is None:
                    raise ValueError("work item not found")
                if (
                    agent_uuid is not None
                    and work_item_row.assigned_agent_id != agent_uuid
                ):
                    raise ValueError("work item is assigned to another agent")
                if agent_uuid is None and work_item_row.assigned_agent_id is not None:
                    raise ValueError("work item requires its typed Agent binding")
                if (
                    revision_uuid is not None
                    and work_item_row.agent_revision_id != revision_uuid
                ):
                    raise ValueError("work item is pinned to another Agent revision")
                if revision_uuid is None and work_item_row.agent_revision_id is not None:
                    raise ValueError("work item requires its pinned AgentRevision")
                if work_item_row.project_id is not None and work_item_row.project_id != parse_uuid(project_id):
                    raise ValueError("work item does not bind to project")
                if work_item_row.task_id is not None and work_item_row.task_id != task_uuid:
                    raise ValueError("work item does not bind to task")
                if work_item_attempt is not None:
                    existing_attempt = (
                        await session.execute(
                            select(AgentRun)
                            .where(
                                AgentRun.work_item_id == work_item_uuid,
                                AgentRun.work_item_attempt == work_item_attempt,
                            )
                            .limit(1)
                        )
                    ).scalars().first()
            safe_manifest = _bounded_execution_manifest(execution_manifest)
            _validate_manifest_bindings(
                safe_manifest,
                agent_id=agent_uuid,
                revision_id=revision_uuid,
                task_id=task_uuid,
                work_item_id=work_item_uuid,
                project_id=parse_uuid(project_id),
            )
            safe_manifest_hash = None
            if safe_manifest is not None:
                computed_manifest_hash = hashlib.sha256(
                    json.dumps(
                        safe_manifest,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                if execution_manifest_hash and str(execution_manifest_hash).strip().lower() != computed_manifest_hash:
                    raise ValueError("execution_manifest_hash does not match manifest")
                safe_manifest_hash = computed_manifest_hash
            elif execution_manifest_hash:
                candidate_hash = str(execution_manifest_hash).strip().lower()
                if not re.fullmatch(r"[0-9a-f]{64}", candidate_hash):
                    raise ValueError("invalid execution_manifest_hash")
                safe_manifest_hash = candidate_hash

            if existing_attempt is not None:
                if (
                    existing_attempt.agent_id != agent_uuid
                    or existing_attempt.agent_revision_id != revision_uuid
                    or existing_attempt.task_id != task_uuid
                    or existing_attempt.project_id != parse_uuid(project_id)
                    or existing_attempt.previous_attempt_run_id != previous_attempt_uuid
                    or existing_attempt.run_type
                    != (str(run_type or "chat_turn").strip() or "chat_turn")
                    or existing_attempt.execution_manifest_hash != safe_manifest_hash
                ):
                    raise ValueError(
                        "work item attempt idempotency binding conflict"
                    )
                return existing_attempt.to_dict()

            if app_id and not base_revision:
                try:
                    from .app_git_service import AppGitService

                    base_revision = AppGitService().status(app_id).get("revision")
                except Exception:
                    # App workspace may not have been initialized yet; the run
                    # remains durable and the missing revision is explicit.
                    base_revision = None

            normalized_run_type = str(run_type or "chat_turn").strip() or "chat_turn"
            if context_hint_uuid is not None:
                metadata = dict(metadata or {})
                metadata["context_project_hint_id"] = str(context_hint_uuid)
            safe_objective = str(objective or "")
            safe_title = str(title or "")[:255]
            if _is_cloud_advisor_run_type(normalized_run_type):
                # A Cloud Advisor query is transient provider input.  Keep a
                # stable marker in AgentRun rather than copying the prompt
                # into the durable objective column.
                safe_objective = (
                    CLOUD_ADVISOR_CONTENT_REDACTED_MARKER
                    if safe_objective
                    else ""
                )
                safe_title = (
                    CLOUD_ADVISOR_CONTENT_REDACTED_MARKER
                    if safe_title
                    else ""
                )

            run_values = dict(
                parent_run_id=parent_uuid,
                root_run_id=root_uuid,
                session_id=parse_uuid(session_id),
                trigger_message_id=parse_uuid(trigger_message_id),
                project_id=parse_uuid(project_id),
                app_id=parse_uuid(app_id),
                app_target_id=parse_uuid(app_target_id),
                agent_id=agent_uuid,
                agent_revision_id=revision_uuid,
                task_id=task_uuid,
                work_item_id=work_item_uuid,
                work_item_attempt=work_item_attempt,
                acting_subagent_id=(
                    str(acting_subagent_id).strip()[:100]
                    if acting_subagent_id
                    else None
                ),
                previous_attempt_run_id=previous_attempt_uuid,
                execution_manifest_hash=safe_manifest_hash,
                base_revision=str(base_revision).strip() if base_revision else None,
                result_revision=str(result_revision).strip() if result_revision else None,
                user_id=str(user_id) if user_id else None,
                client_message_id=(
                    str(client_message_id).strip() if client_message_id else None
                ),
                client_message_key=(
                    dispatch_client_message_key(client_message_id)
                    if client_message_id
                    else None
                ),
                request_fingerprint=request_fingerprint,
                run_type=normalized_run_type,
                status="queued",
                title=safe_title,
                objective=safe_objective,
                generation_profile=generation_profile,
                provider=provider,
                model=model,
                result={},
                validation={},
                run_metadata=_safe_audit_metadata(
                    metadata,
                    run_type=normalized_run_type,
                ),
                created_at=now,
                updated_at=now,
                last_event_at=now,
            )
            # SQLAlchemy's JSON type serializes an explicit ``None`` as JSON
            # ``null`` on some dialects.  The nullable column must receive a
            # real SQL NULL so the migration check permits legacy/human runs
            # that have no execution manifest.
            if safe_manifest is not None:
                run_values["resolved_execution_manifest"] = safe_manifest
            run = AgentRun(**run_values)
            session.add(run)
            await session.flush()
            await self._ensure_interactive_work_in_session(
                session,
                run,
                conversation=locked_conversation,
                user_id=user_id,
                client_message_id=client_message_id,
                metadata=metadata,
            )
            if run.root_run_id is None:
                run.root_run_id = run.id
            session.add(
                AgentRunEvent(
                    run_id=run.id,
                    sequence=1,
                    event_type="run.queued",
                    status="queued",
                    message="Agent run queued",
                    payload={},
                    created_at=now,
                )
            )
            await session.commit()
            await session.refresh(run)
            return run.to_dict()
        except IntegrityError as exc:
            await session.rollback()
            # The partial unique index is the cross-process idempotency fence
            # for one WorkItem attempt. If another worker committed first,
            # return its immutable AgentRun rather than creating a duplicate.
            if work_item_uuid is not None and work_item_attempt is not None:
                try:
                    winner = (
                        await session.execute(
                            select(AgentRun)
                            .where(
                                AgentRun.work_item_id == work_item_uuid,
                                AgentRun.work_item_attempt == work_item_attempt,
                            )
                            .limit(1)
                        )
                    ).scalars().first()
                    if winner is not None:
                        return winner.to_dict()
                except Exception:
                    pass
            if not client_message_id:
                logger.exception("Failed to create agent run")
            raise
        except Exception as exc:
            await session.rollback()
            if not (client_message_id and isinstance(exc, IntegrityError)):
                logger.exception("Failed to create agent run")
            raise
        finally:
            await session.close()

    async def get_dispatch_run(
        self,
        *,
        session_id: str,
        user_id: str,
        client_message_id: str,
    ) -> Dict[str, Any] | None:
        session_uuid = parse_uuid(session_id)
        normalized_client_id = str(client_message_id or "").strip()
        if session_uuid is None or not normalized_client_id:
            return None
        client_message_key = dispatch_client_message_key(normalized_client_id)
        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            return None
        session = await self._session()
        try:
            result = await session.execute(
                select(AgentRun).where(
                    AgentRun.session_id == session_uuid,
                    AgentRun.user_id == normalized_user_id,
                    AgentRun.client_message_key == client_message_key,
                )
            )
            run = result.scalars().first()
            return run.to_dict() if run else None
        finally:
            await session.close()

    async def create_or_get_dispatch_run(
        self,
        *,
        session_id: str,
        user_id: str,
        client_message_id: str,
        **run_kwargs: Any,
    ) -> tuple[Dict[str, Any], bool]:
        """DB unique制約をclaimとして同一mobile dispatchを原子的に一意化する。"""
        normalized_client_id = str(client_message_id or "").strip()
        if not normalized_client_id:
            run = await self.create_run(
                session_id=session_id,
                user_id=user_id,
                **run_kwargs,
            )
            return run, True
        try:
            run = await self.create_run(
                session_id=session_id,
                user_id=user_id,
                client_message_id=normalized_client_id,
                **run_kwargs,
            )
            return run, True
        except IntegrityError:
            # PostgreSQLは競合INSERTのtransaction終了を待ってからunique violationを
            # 返すため、このSELECTはwinnerがcommitした同じrunを取得する。
            existing = await self.get_dispatch_run(
                session_id=session_id,
                user_id=user_id,
                client_message_id=normalized_client_id,
            )
            if existing is None:
                raise
            return existing, False

    async def create_or_get_dispatch_turn(
        self,
        *,
        session_id: str,
        client_message_id: str,
        content: str,
        message_metadata: Dict[str, Any],
        sender_type: str | None,
        sender_id: str | None,
        sender_display_name: str | None,
        edit_message_id: str | None,
        outbox_payload: Dict[str, Any],
        request_fingerprint: str,
        persisted_user_message_id: str | None = None,
        user_id: str | None = None,
        project_id: str | None = None,
        context_project_hint_id: str | None = None,
        app_id: str | None = None,
        app_target_id: str | None = None,
        base_revision: str | None = None,
        result_revision: str | None = None,
        objective: str = "",
        generation_profile: str | None = None,
        metadata: Dict[str, Any] | None = None,
        agent_id: str | None = None,
        agent_revision_id: str | None = None,
        expected_agent_id: Any = _EXPECTED_AGENT_OMITTED,
        expected_agent_provided: bool | None = None,
        task_id: str | None = None,
        work_item_id: str | None = None,
        acting_subagent_id: str | None = None,
        previous_attempt_run_id: str | None = None,
        execution_manifest: Dict[str, Any] | None = None,
        execution_manifest_hash: str | None = None,
        session: Any | None = None,
    ) -> tuple[Dict[str, Any], str, bool]:
        """Create message, run and durable outbox in one transaction.

        The session row lock serializes branch placement. The unique run/outbox
        constraints remain the cross-process idempotency authority.
        """
        session_uuid = parse_uuid(session_id)
        normalized_client_id = str(client_message_id or "").strip()
        normalized_user_id = str(user_id or "").strip()
        normalized_fingerprint = str(request_fingerprint or "").strip()
        if (
            session_uuid is None
            or not normalized_client_id
            or not normalized_user_id
            or len(normalized_fingerprint) != 64
        ):
            raise ValueError(
                "valid session_id, user_id, client_message_id and fingerprint are required"
            )
        client_message_key = dispatch_client_message_key(normalized_client_id)

        owns_session = session is None
        db_session = session or await self._session()
        try:
            conversation_result = await db_session.execute(
                select(ConversationSession)
                .where(
                    ConversationSession.id == session_uuid,
                    ConversationSession.deleted_at.is_(None),
                )
                .with_for_update()
            )
            conversation = conversation_result.scalars().first()
            if conversation is None:
                raise ValueError("Session not found or deleted")
            hint_uuid = parse_uuid(context_project_hint_id)
            if context_project_hint_id and hint_uuid is None:
                raise ValueError("invalid context_project_hint_id")
            if hint_uuid is not None and conversation.project_id is not None:
                raise AgentChatAdmissionError(
                    "AoiDots root session must remain projectless",
                    code="dialogue_root_project_forbidden",
                )
            _check_expected_agent_binding(
                conversation,
                expected_agent_id=expected_agent_id,
                expected_agent_provided=expected_agent_provided,
            )

            existing_probe_result = await db_session.execute(
                select(AgentRun).where(
                    AgentRun.session_id == session_uuid,
                    AgentRun.client_message_key == client_message_key,
                    AgentRun.user_id == normalized_user_id,
                ).limit(1)
            )
            existing_probe = existing_probe_result.scalars().first()
            if existing_probe is not None:
                resolved_binding = await _resolve_existing_binding(
                    db_session,
                    existing_probe,
                    config=self.config,
                ) if existing_probe.agent_id else None
                resolved_agent_uuid = (
                    parse_uuid(existing_probe.agent_id) if existing_probe.agent_id else None
                )
                resolved_revision_uuid = (
                    parse_uuid(existing_probe.agent_revision_id)
                    if existing_probe.agent_revision_id
                    else None
                )
            else:
                resolved_agent_uuid, resolved_revision_uuid, resolved_binding = await _pin_session_agent_binding(
                    db_session,
                    conversation,
                    user_id=normalized_user_id,
                    requested_agent_id=agent_id,
                    requested_revision_id=agent_revision_id,
                    config=self.config,
                )
                if resolved_agent_uuid is not None:
                    agent_id = str(resolved_agent_uuid)
                    agent_revision_id = str(resolved_revision_uuid)

            existing_result = await db_session.execute(
                select(AgentRun).where(
                    AgentRun.session_id == session_uuid,
                    AgentRun.client_message_key == client_message_key,
                )
            )
            matching_runs = list(existing_result.scalars().all())
            existing = next(
                (
                    run
                    for run in matching_runs
                    if str(run.user_id or "") == normalized_user_id
                ),
                matching_runs[0] if matching_runs else None,
            )
            if existing is not None:
                if str(existing.user_id or "") != normalized_user_id:
                    raise DispatchConflictError(
                        "client_message_id belongs to another principal"
                    )
                if (
                    existing.request_fingerprint
                    and existing.request_fingerprint != normalized_fingerprint
                ):
                    raise DispatchConflictError(
                        "client_message_id was reused with a different request"
                    )
                requested_agent_uuid = parse_uuid(agent_id)
                requested_revision_uuid = parse_uuid(agent_revision_id)
                if agent_id and requested_agent_uuid is None:
                    raise ValueError("invalid agent_id")
                if agent_revision_id and requested_revision_uuid is None:
                    raise ValueError("invalid agent_revision_id")
                if agent_id and existing.agent_id != requested_agent_uuid:
                    raise DispatchConflictError("client_message_id was reused for another Agent")
                if agent_revision_id and existing.agent_revision_id != requested_revision_uuid:
                    raise DispatchConflictError("client_message_id was reused for another Agent revision")
                requested_task_uuid = parse_uuid(task_id)
                if task_id and requested_task_uuid is None:
                    raise ValueError("invalid task_id")
                if task_id and existing.task_id != requested_task_uuid:
                    raise DispatchConflictError("client_message_id was reused for another Task")
                requested_work_item_uuid = parse_uuid(work_item_id)
                if work_item_id and requested_work_item_uuid is None:
                    raise ValueError("invalid work_item_id")
                if work_item_id and existing.work_item_id != requested_work_item_uuid:
                    raise DispatchConflictError("client_message_id was reused for another WorkItem")
                if acting_subagent_id and existing.acting_subagent_id != str(acting_subagent_id).strip():
                    raise DispatchConflictError("client_message_id was reused for another Subagent")
                if existing.trigger_message_id is None:
                    raise RuntimeError("idempotent dispatch run is incomplete")
                # A historical human Chat run is intentionally independent of
                # a later session Agent selection.  Returning an idempotent
                # replay must never retrofit an Agent identity or create Work
                # for that old run.  Only typed Agent runs may repair their
                # exact immutable binding/Work projection.
                if existing.agent_id is not None:
                    await self._ensure_interactive_work_in_session(
                        db_session,
                        existing,
                        conversation=conversation,
                        user_id=normalized_user_id,
                        client_message_id=normalized_client_id,
                        metadata=metadata,
                        binding=resolved_binding,
                    )
                if owns_session:
                    await db_session.commit()
                else:
                    await db_session.flush()
                return existing.to_dict(), str(existing.trigger_message_id), False

            message = None
            message_uuid = parse_uuid(persisted_user_message_id)
            if persisted_user_message_id:
                if message_uuid is None:
                    raise ValueError("invalid persisted user message")
                persisted_message = (
                    await db_session.execute(
                        select(ConversationMessage).where(
                            ConversationMessage.id == message_uuid,
                            ConversationMessage.session_id == session_uuid,
                            ConversationMessage.role == "user",
                        )
                    )
                ).scalars().first()
                if persisted_message is None:
                    raise ValueError("persisted user message does not match session")
            else:
                from ..memory.conversation_repository import ConversationRepository

                repository = ConversationRepository(db_session)
                await repository._ensure_linear_parent_links(db_session, session_id)
                parent_message_id = None
                branch_index = 0
                if edit_message_id:
                    edit_uuid = parse_uuid(edit_message_id)
                    original = (
                        await db_session.execute(
                            select(ConversationMessage).where(
                                ConversationMessage.id == edit_uuid,
                                ConversationMessage.session_id == session_uuid,
                                ConversationMessage.role == "user",
                            )
                        )
                    ).scalars().first()
                    if original is None:
                        raise ValueError("edit message does not match session")
                    await repository._deactivate_branch_from_message(
                        db_session,
                        str(original.id),
                    )
                    parent_message_id = original.parent_message_id
                    branch_index = await repository._count_branch_siblings(
                        db_session,
                        session_id,
                        str(parent_message_id) if parent_message_id else None,
                    )
                else:
                    parent = await repository._latest_active_message(
                        db_session,
                        session_id,
                    )
                    parent_message_id = parent.id if parent else None
                    branch_index = await repository._count_branch_siblings(
                        db_session,
                        session_id,
                        str(parent_message_id) if parent_message_id else None,
                    )

                message_uuid = uuid.uuid4()
                message = ConversationMessage(
                    id=message_uuid,
                    session_id=session_uuid,
                    role="user",
                    content=content,
                    parent_message_id=parent_message_id,
                    branch_index=branch_index,
                    is_active_branch=True,
                    message_metadata=_jsonable(message_metadata),
                    sender_type=sender_type,
                    sender_id=sender_id,
                    sender_display_name=sender_display_name,
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )

            if app_id and not base_revision:
                try:
                    from .app_git_service import AppGitService

                    base_revision = AppGitService().status(app_id).get("revision")
                except Exception:
                    base_revision = None

            now = datetime.utcnow()
            run_id = uuid.uuid4()
            typed_agent_uuid = parse_uuid(agent_id)
            typed_revision_uuid = parse_uuid(agent_revision_id)
            typed_task_uuid = parse_uuid(task_id)
            typed_work_item_uuid = parse_uuid(work_item_id)
            typed_previous_uuid = parse_uuid(previous_attempt_run_id)
            if agent_id and typed_agent_uuid is None:
                raise ValueError("invalid agent_id")
            if agent_revision_id and typed_revision_uuid is None:
                raise ValueError("invalid agent_revision_id")
            if task_id and typed_task_uuid is None:
                raise ValueError("invalid task_id")
            if work_item_id and typed_work_item_uuid is None:
                raise ValueError("invalid work_item_id")
            if previous_attempt_run_id and typed_previous_uuid is None:
                raise ValueError("invalid previous_attempt_run_id")
            if typed_agent_uuid is not None and typed_revision_uuid is None:
                raise ValueError("typed Agent runs require an exact AgentRevision")
            # ``user_id`` is a legacy human-only field.  Reject any UUID that
            # already belongs to an Agent even when this dispatch payload has
            # no typed agent marker, preventing silent Agent impersonation.
            normalized_user_uuid = parse_uuid(normalized_user_id)
            if normalized_user_uuid is not None:
                user_agent_row = await _lookup_agent_for_legacy_user(
                    db_session, normalized_user_uuid
                )
                if user_agent_row is not None:
                    raise ValueError("Agent IDs must not be stored in user_id")
            typed_agent_row = None
            if typed_agent_uuid is not None:
                typed_agent_row = await db_session.get(Agent, typed_agent_uuid)
                if typed_agent_row is None or str(typed_agent_row.state or "") != "active":
                    raise ValueError("agent is not active")
                if normalized_user_id == str(typed_agent_uuid) or parse_uuid(normalized_user_id) == typed_agent_uuid:
                    raise ValueError("Agent IDs must not be stored in user_id")
            typed_revision_row = None
            if typed_revision_uuid is not None:
                typed_revision_row = await db_session.get(AgentRevision, typed_revision_uuid)
                if typed_revision_row is None or typed_revision_row.agent_id != typed_agent_uuid:
                    raise ValueError("agent revision is not bound to agent")
            _validate_typed_agent_actor(
                user_id=normalized_user_id,
                agent_id=typed_agent_uuid,
                agent_revision=typed_revision_row,
                acting_subagent_id=acting_subagent_id,
                agent_row=typed_agent_row,
                config=self.config,
            )
            if typed_previous_uuid is not None:
                previous_row = await db_session.get(AgentRun, typed_previous_uuid)
                if previous_row is None or (
                    typed_agent_uuid is not None
                    and previous_row.agent_id != typed_agent_uuid
                ):
                    raise ValueError("previous attempt run is not bound to agent")
            if typed_task_uuid is not None:
                task_row = await db_session.get(Task, typed_task_uuid)
                if task_row is None:
                    raise ValueError("task not found")
                project_uuid = parse_uuid(project_id)
                if project_uuid is not None and task_row.project_id != project_uuid:
                    raise ValueError("task does not belong to project")
            if typed_work_item_uuid is not None:
                typed_work_item = await db_session.get(AgentWorkItem, typed_work_item_uuid)
                if typed_work_item is None:
                    raise ValueError("work item not found")
                if (
                    typed_agent_uuid is not None
                    and typed_work_item.assigned_agent_id != typed_agent_uuid
                ):
                    raise ValueError("work item is assigned to another agent")
                if (
                    typed_agent_uuid is None
                    and typed_work_item.assigned_agent_id is not None
                ):
                    raise ValueError("work item requires its typed Agent binding")
                if (
                    typed_revision_uuid is not None
                    and typed_work_item.agent_revision_id != typed_revision_uuid
                ):
                    raise ValueError("work item is pinned to another Agent revision")
                if (
                    typed_revision_uuid is None
                    and typed_work_item.agent_revision_id is not None
                ):
                    raise ValueError("work item requires its pinned AgentRevision")
                if typed_work_item.project_id is not None and typed_work_item.project_id != parse_uuid(project_id):
                    raise ValueError("work item does not bind to project")
                if typed_work_item.task_id is not None and typed_work_item.task_id != typed_task_uuid:
                    raise ValueError("work item does not bind to task")
            safe_manifest = _bounded_execution_manifest(execution_manifest)
            _validate_manifest_bindings(
                safe_manifest,
                agent_id=typed_agent_uuid,
                revision_id=typed_revision_uuid,
                task_id=typed_task_uuid,
                work_item_id=typed_work_item_uuid,
                project_id=parse_uuid(project_id),
            )
            manifest_hash = None
            if safe_manifest is not None:
                manifest_hash = hashlib.sha256(
                    json.dumps(safe_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                if execution_manifest_hash and str(execution_manifest_hash).strip().lower() != manifest_hash:
                    raise ValueError("execution_manifest_hash does not match manifest")
            elif execution_manifest_hash:
                if not re.fullmatch(r"[0-9a-f]{64}", str(execution_manifest_hash).strip().lower()):
                    raise ValueError("invalid execution_manifest_hash")
                manifest_hash = str(execution_manifest_hash).strip().lower()
            durable_payload = {
                **outbox_payload,
                "agent_run_id": str(run_id),
                "persisted_user_message_id": str(message_uuid),
                "skip_user_persistence": True,
            }
            run_metadata = dict(metadata or {})
            run_values = dict(
                id=run_id,
                root_run_id=run_id,
                session_id=session_uuid,
                trigger_message_id=message_uuid,
                project_id=parse_uuid(project_id),
                app_id=parse_uuid(app_id),
                app_target_id=parse_uuid(app_target_id),
                agent_id=typed_agent_uuid,
                agent_revision_id=typed_revision_uuid,
                task_id=typed_task_uuid,
                work_item_id=typed_work_item_uuid,
                acting_subagent_id=str(acting_subagent_id).strip()[:100] if acting_subagent_id else None,
                previous_attempt_run_id=typed_previous_uuid,
                execution_manifest_hash=manifest_hash,
                base_revision=str(base_revision).strip() if base_revision else None,
                result_revision=str(result_revision).strip() if result_revision else None,
                user_id=normalized_user_id,
                client_message_id=normalized_client_id,
                client_message_key=client_message_key,
                request_fingerprint=normalized_fingerprint,
                run_type="chat_turn",
                status="queued",
                title="",
                objective=str(objective or ""),
                generation_profile=generation_profile,
                result={},
                validation={},
                run_metadata=_safe_audit_metadata(run_metadata, run_type="chat_turn"),
                created_at=now,
                updated_at=now,
                last_event_at=now,
            )
            if safe_manifest is not None:
                run_values["resolved_execution_manifest"] = safe_manifest
            run = AgentRun(**run_values)
            outbox = ConversationDispatchOutbox(
                run_id=run_id,
                session_id=session_uuid,
                user_id=normalized_user_id,
                client_message_id=normalized_client_id,
                client_message_key=client_message_key,
                request_fingerprint=normalized_fingerprint,
                payload=_jsonable(durable_payload),
                status="pending",
                attempts=0,
                created_at=now,
                updated_at=now,
            )
            if message is not None:
                # agent_runs.trigger_message_id は conversation_messages への
                # FK。同一 flush に混ぜると run が先に INSERT されて
                # ForeignKeyViolation になるため、user message を先に確定させる。
                db_session.add(message)
                await db_session.flush()
                from ..features import Features

                if Features.virtual_company() and Features.autonomous_agent_runtime():
                    from .agent_automation_events import record_chat_message_event

                    await record_chat_message_event(db_session, message, conversation)
            db_session.add(run)
            await db_session.flush()
            # Root Dialogue continuations reserve the shared chain budget
            # before Work admission/provider dispatch.  The helper is a
            # no-op for ordinary legacy Chat runs and for worker markers.
            root_budget_blocked = False
            root_budget_block_code: str | None = None
            try:
                await self._admit_root_dialogue_budget_in_session(
                    db_session,
                    run,
                    metadata=run_metadata,
                    binding=resolved_binding,
                )
            except AgentChatAdmissionError as exc:
                if exc.code not in _ROOT_DIALOGUE_BUDGET_BLOCK_CODES:
                    raise
                root_budget_blocked = True
                root_budget_block_code = exc.code
                await self._materialize_root_dialogue_budget_blocked_in_session(
                    db_session,
                    run,
                    metadata=run_metadata,
                    binding=resolved_binding,
                    error_code=exc.code,
                )
            if not root_budget_blocked:
                await self._ensure_interactive_work_in_session(
                    db_session,
                    run,
                    conversation=conversation,
                    user_id=normalized_user_id,
                    client_message_id=normalized_client_id,
                    metadata=(
                        run.run_metadata
                        if isinstance(run.run_metadata, Mapping)
                        else run_metadata
                    ),
                    binding=resolved_binding,
                )
            else:
                outbox.status = "cancelled"
                outbox.updated_at = now
            db_session.add_all(
                [
                    outbox,
                    AgentRunEvent(
                        run_id=run_id,
                        sequence=1,
                        event_type=("run.failed" if root_budget_blocked else "run.queued"),
                        status=("failed" if root_budget_blocked else "queued"),
                        message=(
                            "dialogue_budget_blocked"
                            if root_budget_blocked
                            else "Agent run queued"
                        ),
                        payload=(
                            {
                                "safe_error_code": root_budget_block_code,
                                "outcome_classification": "blocked",
                                "provider_started": False,
                                "dialogue_id": (
                                    str(
                                        parse_uuid(
                                            (run_metadata.get("agent_dialogue") or {}).get(
                                                "dialogue_id"
                                            )
                                        )
                                    )
                                    if isinstance(run_metadata.get("agent_dialogue"), Mapping)
                                    and parse_uuid(
                                        (run_metadata.get("agent_dialogue") or {}).get(
                                            "dialogue_id"
                                        )
                                    )
                                    is not None
                                    else None
                                ),
                            }
                            if root_budget_blocked
                            else {}
                        ),
                        created_at=now,
                    ),
                ]
            )
            # AgentDialogue transport supplies a typed source reference.  Keep
            # the child Run independent (parent_run_id remains NULL), while
            # recording the validated cross-Agent causality in the same
            # message/Run/Work/outbox transaction.
            dialogue_ref = metadata.get("agent_dialogue") if isinstance(metadata, Mapping) else None
            if (
                isinstance(outbox_payload, Mapping)
                and outbox_payload.get("_dialogue_transport")
                and isinstance(dialogue_ref, Mapping)
            ):
                parent_uuid = parse_uuid(dialogue_ref.get("source_run_id"))
                source_agent_uuid = parse_uuid(dialogue_ref.get("source_agent_id"))
                source_session_uuid = parse_uuid(dialogue_ref.get("source_session_id"))
                if parent_uuid is None or source_agent_uuid is None or source_session_uuid is None:
                    raise AgentChatAdmissionError(
                        "dialogue source provenance is incomplete",
                        code="dialogue_source_provenance_incomplete",
                    )
                source_run = await db_session.get(AgentRun, parent_uuid)
                if (
                    source_run is None
                    or source_run.parent_run_id is not None
                    or source_run.agent_id != source_agent_uuid
                    or source_run.session_id != source_session_uuid
                    or str(source_run.user_id or "") != normalized_user_id
                ):
                    raise AgentChatAdmissionError(
                        "dialogue source provenance is invalid",
                        code="dialogue_source_provenance_invalid",
                    )
                db_session.add(
                    AgentRunEdge(
                        parent_run_id=parent_uuid,
                        child_run_id=run_id,
                        purpose="agent_dialogue",
                        status="open",
                        edge_metadata=_safe_audit_metadata(
                            {
                                "dialogue_id": dialogue_ref.get("dialogue_id"),
                                "delivery_id": dialogue_ref.get("delivery_id"),
                                "source_agent_id": str(source_agent_uuid),
                                "receiver_agent_id": str(typed_agent_uuid) if typed_agent_uuid else None,
                                "control_revision": dialogue_ref.get("expected_control_revision"),
                            }
                        ),
                        created_at=now,
                    )
                )
            if message is not None:
                await db_session.execute(
                    update(ConversationSession)
                    .where(ConversationSession.id == session_uuid)
                    .values(
                        message_count=ConversationSession.message_count + 1,
                        last_activity=_monotonic_activity(now),
                        # App開発チャットに限らず、エージェント実行中は
                        # サイドバーのアイコンを進行中表示へ切り替える。
                        # Session status is constrained to the existing UI
                        # contract.  A budget-blocked Run/Work remains typed
                        # ``blocked`` in its own ledger/projection.
                        development_status=(
                            "waiting_for_user" if root_budget_blocked else "working"
                        ),
                    )
                )
            if owns_session:
                await db_session.commit()
            else:
                await db_session.flush()
            return run.to_dict(), str(message_uuid), True
        except IntegrityError:
            if not owns_session:
                raise
            await db_session.rollback()
            existing = await self.get_dispatch_run(
                session_id=session_id,
                user_id=normalized_user_id,
                client_message_id=normalized_client_id,
            )
            if existing is None or not existing.get("trigger_message_id"):
                raise
            if (
                existing.get("request_fingerprint")
                and existing["request_fingerprint"] != normalized_fingerprint
            ):
                raise DispatchConflictError(
                    "client_message_id was reused with a different request"
                )
            return existing, str(existing["trigger_message_id"]), False
        except Exception:
            if owns_session:
                await db_session.rollback()
            raise
        finally:
            if owns_session:
                await db_session.close()

    async def create_server_dispatch_turn(
        self,
        *,
        session_id: str,
        user_id: str,
        request_fingerprint: str,
        **turn_kwargs: Any,
    ) -> tuple[Dict[str, Any], str, bool]:
        """Use the atomic dispatch transaction for legacy no-client-ID sends.

        Legacy callers had no client retry token.  Generate a server-owned
        per-request key and route through the same message/Run/Work/outbox
        transaction, preserving the old non-idempotent retry semantics while
        preventing a pre-saved user message from orphaning a failed Run.
        """

        server_key = f"server:{uuid.uuid4()}"
        return await self.create_or_get_dispatch_turn(
            session_id=session_id,
            user_id=user_id,
            client_message_id=server_key,
            request_fingerprint=request_fingerprint,
            **turn_kwargs,
        )

    async def list_recoverable_dispatch_run_ids(
        self,
        *,
        limit: int = 50,
    ) -> list[str]:
        """List pending or expired deliveries; claim remains a separate CAS."""
        now = datetime.utcnow()
        session = await self._session()
        try:
            result = await session.execute(
                select(ConversationDispatchOutbox.run_id)
                .where(
                    or_(
                        ConversationDispatchOutbox.status == "pending",
                        and_(
                            ConversationDispatchOutbox.status == "claimed",
                            ConversationDispatchOutbox.lease_expires_at < now,
                        ),
                    )
                )
                .order_by(ConversationDispatchOutbox.created_at)
                .limit(max(1, min(int(limit), 500)))
            )
            return [str(run_id) for run_id in result.scalars().all()]
        finally:
            await session.close()

    async def purge_delivered_dispatches(
        self,
        *,
        older_than_seconds: float = DISPATCH_OUTBOX_RETENTION_SECONDS,
        limit: int = 500,
    ) -> int:
        """Bound outbox growth without removing AgentRun idempotency records."""
        cutoff = datetime.utcnow() - timedelta(
            seconds=max(0.0, older_than_seconds)
        )
        candidate_run_ids = (
            select(ConversationDispatchOutbox.run_id)
            .where(
                ConversationDispatchOutbox.status == "delivered",
                ConversationDispatchOutbox.delivered_at.is_not(None),
                ConversationDispatchOutbox.delivered_at < cutoff,
            )
            .order_by(ConversationDispatchOutbox.delivered_at)
            .limit(max(1, min(int(limit), 5_000)))
        )
        session = await self._session()
        try:
            result = await session.execute(
                delete(ConversationDispatchOutbox).where(
                    ConversationDispatchOutbox.run_id.in_(candidate_run_ids)
                )
            )
            await session.commit()
            return int(result.rowcount or 0)
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def claim_dispatch_delivery(
        self,
        *,
        run_id: str,
        lease_seconds: float = 5.0,
        max_attempts: int = DISPATCH_MAX_ATTEMPTS,
    ) -> Dict[str, Any] | None:
        run_uuid = parse_uuid(run_id)
        if run_uuid is None:
            return None
        now = datetime.utcnow()
        lease_token = str(uuid.uuid4())
        safe_max_attempts = max(1, min(int(max_attempts or DISPATCH_MAX_ATTEMPTS), 100))
        session = await self._session()
        try:
            result = await session.execute(
                update(ConversationDispatchOutbox)
                .where(
                    ConversationDispatchOutbox.run_id == run_uuid,
                    or_(
                        ConversationDispatchOutbox.status == "pending",
                        and_(
                            ConversationDispatchOutbox.status == "claimed",
                            ConversationDispatchOutbox.lease_expires_at < now,
                        ),
                    ),
                    ConversationDispatchOutbox.attempts < safe_max_attempts,
                )
                .values(
                    status="claimed",
                    lease_owner=_DISPATCH_PROCESS_ID,
                    lease_token=lease_token,
                    lease_expires_at=now
                    + timedelta(seconds=max(0.1, lease_seconds)),
                    attempts=ConversationDispatchOutbox.attempts + 1,
                    updated_at=now,
                )
                .returning(ConversationDispatchOutbox.payload)
            )
            payload = result.scalar_one_or_none()
            await session.commit()
            if payload is None:
                # If the row exists but exhausted its retry budget, transition
                # it to a durable dead letter and terminalize the run.  A
                # missing/non-eligible row remains a harmless no-op.
                await self.deadletter_dispatch(
                    run_id,
                    reason="Dispatch retry budget exhausted",
                    max_attempts=safe_max_attempts,
                )
                return None
            return {
                "lease_token": lease_token,
                "lease_owner": _DISPATCH_PROCESS_ID,
                "lease_expires_at": now + timedelta(seconds=max(0.1, lease_seconds)),
                "run_id": str(run_uuid),
                "payload": dict(payload or {}),
            }
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    @staticmethod
    def _dispatch_has_provider_evidence(
        run: AgentRun,
        outbox: ConversationDispatchOutbox,
    ) -> bool:
        for evidence in (
            run.result if isinstance(run.result, Mapping) else {},
            run.run_metadata if isinstance(run.run_metadata, Mapping) else {},
            outbox.payload if isinstance(outbox.payload, Mapping) else {},
        ):
            if evidence.get("provider_started") is True:
                return True
            if isinstance(evidence.get("usage"), Mapping):
                return True
            marker = evidence.get("agent_dialogue")
            if isinstance(marker, Mapping):
                if marker.get("provider_started") is True:
                    return True
                if isinstance(marker.get("usage"), Mapping):
                    return True
        return False

    async def _dispatch_has_dialogue_usage_receipt_in_session(
        self,
        session: Any,
        run: AgentRun,
    ) -> bool:
        """Read the exact Dialogue/Run receipt before Work settlement."""

        metadata = run.run_metadata if isinstance(run.run_metadata, Mapping) else {}
        marker = metadata.get("agent_dialogue")
        dialogue_id = (
            parse_uuid(marker.get("dialogue_id"))
            if isinstance(marker, Mapping)
            else None
        )
        if dialogue_id is None:
            return False
        try:
            dialogue = (
                await session.execute(
                    select(AgentDialogue)
                    .where(AgentDialogue.id == dialogue_id)
                    .with_for_update()
                )
            ).scalars().first()
            if dialogue is None:
                return True
            from .agent_dialogue_budget import get_dialogue_budget_owner

            budget_owner = await get_dialogue_budget_owner(session, dialogue, lock=True)
            budget_root_id = parse_uuid(
                getattr(budget_owner, "root_dialogue_id", None)
            )
            receipt = (
                await session.execute(
                    select(AgentDialogueUsage)
                    .where(
                        (
                            AgentDialogueUsage.budget_root_dialogue_id
                            == budget_root_id
                            if budget_root_id is not None
                            else AgentDialogueUsage.dialogue_id == dialogue_id
                        ),
                        AgentDialogueUsage.run_id == run.id,
                    )
                    .with_for_update()
                )
            ).scalars().first()
            return receipt is not None
        except OperationalError:
            # An unavailable accounting table cannot prove absence of a
            # receipt; retain the reservation and fail closed.
            return True

    async def _release_unstarted_dialogue_dispatch_reservation_in_session(
        self,
        session: Any,
        run: AgentRun,
        outbox: ConversationDispatchOutbox,
        *,
        safe_reason: str,
        max_attempts: int,
    ) -> bool:
        """Release one proven pre-provider dispatch reservation.

        Dispatch retries are delivery attempts, not provider attempts.  A
        dead-lettered Run may therefore still own a token reservation.  This
        helper accepts only an exact terminal Run/Work/Dialogue binding whose
        provider-start and usage evidence are both absent; charged, unknown,
        live, or mismatched rows remain fail-closed.
        """

        if (
            str(getattr(outbox, "status", "") or "").casefold()
            != DISPATCH_DEADLETTER_STATUS
            or int(getattr(outbox, "attempts", 0) or 0) < max_attempts
            or str(getattr(run, "status", "") or "").casefold() != "failed"
            or getattr(run, "ended_at", None) is None
            or getattr(run, "started_at", None) is not None
            or str(getattr(run, "run_type", "") or "").casefold() != "chat_turn"
            or getattr(run, "parent_run_id", None) is not None
            or str(getattr(run, "error", "") or "") != safe_reason
        ):
            return False

        run_result = run.result if isinstance(run.result, Mapping) else {}
        run_metadata = run.run_metadata if isinstance(run.run_metadata, Mapping) else {}
        marker = run_metadata.get("agent_dialogue")
        if not isinstance(marker, Mapping):
            return False
        dialogue_id = parse_uuid(marker.get("dialogue_id"))
        role = str(marker.get("role") or "").strip().casefold()
        if dialogue_id is None or role not in {"root", "worker"}:
            return False

        for evidence in (run_result, run_metadata, marker, outbox.payload):
            if not isinstance(evidence, Mapping):
                continue
            if evidence.get("provider_started") is True:
                return False
            if isinstance(evidence.get("usage"), Mapping):
                return False

        dialogue = (
            await session.execute(
                select(AgentDialogue)
                .where(AgentDialogue.id == dialogue_id)
                .with_for_update()
            )
        ).scalars().first()
        if dialogue is None:
            return False
        from .agent_dialogue_budget import get_dialogue_budget_owner

        try:
            budget_owner = await get_dialogue_budget_owner(
                session,
                dialogue,
                lock=True,
            )
        except Exception:
            return False
        budget_root_id = parse_uuid(getattr(budget_owner, "root_dialogue_id", None))
        usage_receipt = (
            await session.execute(
                select(AgentDialogueUsage)
                .where(
                    (
                        AgentDialogueUsage.budget_root_dialogue_id == budget_root_id
                        if budget_root_id is not None
                        else AgentDialogueUsage.dialogue_id == dialogue.id
                    ),
                    AgentDialogueUsage.run_id == run.id,
                )
                .with_for_update()
            )
        ).scalars().first()
        # Even an unknown/cost_unknown/token_unknown receipt is evidence that
        # this exact Run reached the accounting boundary; retain its
        # reservation and let the normal fail-closed path handle recovery.
        if usage_receipt is not None:
            return False
        reservation_key = f"run:{run.id}"
        reservation_query = select(AgentDialogueReservation).where(
            (
                AgentDialogueReservation.budget_root_dialogue_id == budget_root_id
                if budget_root_id is not None
                else AgentDialogueReservation.dialogue_id == dialogue.id
            ),
            AgentDialogueReservation.reservation_key == reservation_key,
        ).with_for_update()
        reservation = (await session.execute(reservation_query)).scalars().first()
        if reservation is None:
            return False
        work = None
        if getattr(run, "work_item_id", None) is not None:
            work = (
                await session.execute(
                    select(AgentWorkItem)
                    .where(AgentWorkItem.id == run.work_item_id)
                    .with_for_update()
                )
            ).scalars().first()
        if dialogue is None or work is None:
            return False

        expected_session = (
            dialogue.root_session_id if role == "root" else dialogue.worker_session_id
        )
        expected_agent = (
            dialogue.root_agent_id if role == "root" else dialogue.worker_agent_id
        )
        expected_revision = (
            dialogue.root_agent_revision_id
            if role == "root"
            else dialogue.worker_agent_revision_id
        )
        if (
            parse_uuid(run.session_id) != parse_uuid(expected_session)
            or parse_uuid(run.agent_id) != parse_uuid(expected_agent)
            or parse_uuid(run.agent_revision_id) != parse_uuid(expected_revision)
            or str(run.user_id or "") != str(dialogue.owner_user_id or "")
            or parse_uuid(work.id) != parse_uuid(run.work_item_id)
            or parse_uuid(work.conversation_session_id) != parse_uuid(expected_session)
            or parse_uuid(work.assigned_agent_id) != parse_uuid(expected_agent)
            or parse_uuid(work.agent_revision_id) != parse_uuid(expected_revision)
            or str(work.source_type or "") != "chat_turn"
            or str(work.execution_adapter or "") != "interactive_chat"
            or str(work.intent_key or "") != "interactive_chat"
            or str(work.source_id or "") != str(run.id)
            or str(work.source_revision or "") != str(run.agent_revision_id or "")
            or str(work.state or "").casefold()
            not in {"pending", "failed", "blocked", "cancelled"}
            or int(work.attempt_count or 0) != 0
            or getattr(work, "started_at", None) is not None
            or getattr(work, "active_agent_run_id", None) not in (None, run.id)
            or getattr(work, "lease_owner", None) is not None
            or getattr(work, "lease_token", None) is not None
            or getattr(work, "lease_expires_at", None) is not None
        ):
            return False

        if (
            parse_uuid(reservation.dialogue_id) != parse_uuid(dialogue.id)
            or parse_uuid(reservation.run_id) != parse_uuid(run.id)
            or int(reservation.charged_tokens or 0) != 0
            or int(reservation.charged_cost_micros or 0) != 0
        ):
            return False
        if str(reservation.state or "").casefold() != "reserved":
            return False

        from .agent_dialogue_budget import release_dialogue_budget

        await release_dialogue_budget(session, dialogue.id, reservation_key)
        return True

    async def deadletter_dispatch(
        self,
        run_id: str | None,
        *,
        reason: str = "Dispatch retry budget exhausted",
        max_attempts: int = DISPATCH_MAX_ATTEMPTS,
    ) -> bool:
        """Permanently settle an exhausted dispatch/outbox row.

        The existing outbox table is retained as the idempotency authority;
        ``deadletter`` is an additive status and the associated AgentRun gets
        one terminal failure event.  Repeated calls are no-ops.
        """

        run_uuid = parse_uuid(run_id)
        if run_uuid is None:
            return False
        now = datetime.utcnow()
        safe_reason = sanitize_durable_error_text(reason)
        safe_max_attempts = max(1, min(int(max_attempts or DISPATCH_MAX_ATTEMPTS), 100))
        session = await self._session()
        try:
            run = await session.get(AgentRun, run_uuid, with_for_update=True)
            outbox = (
                await session.execute(
                    select(ConversationDispatchOutbox)
                    .where(ConversationDispatchOutbox.run_id == run_uuid)
                    .with_for_update()
                )
            ).scalars().first()
            if outbox is None:
                return False
            if outbox.status == DISPATCH_DEADLETTER_STATUS:
                evidence_present = (
                    run is not None
                    and self._dispatch_has_provider_evidence(run, outbox)
                )
                receipt_present = (
                    await self._dispatch_has_dialogue_usage_receipt_in_session(
                        session, run
                    )
                    if run is not None and not evidence_present
                    else False
                )
                evidence_present = evidence_present or receipt_present
                released = (
                    await self._release_unstarted_dialogue_dispatch_reservation_in_session(
                        session,
                        run,
                        outbox,
                        safe_reason=safe_reason,
                        max_attempts=safe_max_attempts,
                    )
                    if run is not None and not evidence_present
                    else False
                )
                settled = (
                    await self._settle_linked_work_in_session(
                        session,
                        run,
                        status="failed",
                        message=safe_reason,
                        result={"source": "dispatch_deadletter_reconcile", "run_id": str(run_uuid)},
                        require_exact_interactive=True,
                        require_never_started=True,
                    )
                    if run is not None and not evidence_present
                    else False
                )
                if settled or released:
                    await session.commit()
                else:
                    await session.rollback()
                return settled or released
            if int(outbox.attempts or 0) < safe_max_attempts:
                return False
            if outbox.status not in {"pending", "claimed"}:
                return False
            outbox.status = DISPATCH_DEADLETTER_STATUS
            outbox.lease_owner = None
            outbox.lease_token = None
            outbox.lease_expires_at = None
            outbox.updated_at = now
            if run is not None and run.status not in RUN_TERMINAL_STATUSES:
                run.status = "failed"
                run.error = safe_reason
                run.ended_at = run.ended_at or now
                run.updated_at = now
                run.result = {
                    **(dict(run.result) if isinstance(run.result, Mapping) else {}),
                    "source": "dispatch_deadletter",
                    "run_id": str(run.id),
                    "safe_error_code": "dispatch_deadletter",
                    "outcome_classification": "permanent",
                }
                await self._append_event(
                    session,
                    run,
                    "dispatch.deadlettered",
                    status="failed",
                    message=safe_reason,
                    payload={
                        "attempts": int(outbox.attempts or 0),
                        "max_attempts": safe_max_attempts,
                        "outbox_status": DISPATCH_DEADLETTER_STATUS,
                    },
                )
                await self._append_event(
                    session,
                    run,
                    "run.failed",
                    status="failed",
                    message=safe_reason,
                    payload={"reason": "dispatch_deadletter"},
                )
            if run is not None:
                # A dead-lettered ConversationDispatchOutbox may leave the
                # ordinary interactive Chat Work pending with this exact Run
                # still attached.  Settle only that proven interactive Work
                # in the same transaction; never touch a live/reclaimed or
                # uncertain provider attempt.
                evidence_present = self._dispatch_has_provider_evidence(run, outbox)
                if not evidence_present:
                    evidence_present = (
                        await self._dispatch_has_dialogue_usage_receipt_in_session(
                            session, run
                        )
                    )
                released = await self._release_unstarted_dialogue_dispatch_reservation_in_session(
                    session,
                    run,
                    outbox,
                    safe_reason=safe_reason,
                    max_attempts=safe_max_attempts,
                ) if not evidence_present else False
                if released:
                    run.result = {
                        **(dict(run.result) if isinstance(run.result, Mapping) else {}),
                        "provider_started": False,
                    }
                    marker = (
                        run.run_metadata.get("agent_dialogue")
                        if isinstance(run.run_metadata, Mapping)
                        else None
                    )
                    if isinstance(marker, Mapping):
                        metadata_copy = dict(run.run_metadata)
                        marker_copy = dict(marker)
                        marker_copy["provider_started"] = False
                        metadata_copy["agent_dialogue"] = marker_copy
                        run.run_metadata = _safe_audit_metadata(
                            metadata_copy,
                            run_type=run.run_type,
                        )
                if not evidence_present:
                    await self._settle_linked_work_in_session(
                        session,
                        run,
                        status="failed",
                        message=safe_reason,
                        result={
                            "source": "dispatch_deadletter",
                            "run_id": str(run.id),
                            **({"safe_error_code": "dispatch_deadletter"} if released else {}),
                        },
                        require_exact_interactive=True,
                        require_never_started=True,
                    )
            await session.commit()
            return True
        except Exception:
            await session.rollback()
            logger.warning("Failed to dead-letter dispatch: %s", run_id, exc_info=True)
            return False
        finally:
            await session.close()

    async def mark_dispatch_delivered(
        self,
        *,
        run_id: str,
        lease_token: str,
    ) -> bool:
        run_uuid = parse_uuid(run_id)
        if run_uuid is None:
            return False
        now = datetime.utcnow()
        session = await self._session()
        try:
            result = await session.execute(
                update(ConversationDispatchOutbox)
                .where(
                    ConversationDispatchOutbox.run_id == run_uuid,
                    ConversationDispatchOutbox.status == "claimed",
                    ConversationDispatchOutbox.lease_token == lease_token,
                )
                .values(
                    status="delivered",
                    payload={},
                    delivered_at=now,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
            await session.commit()
            return bool(result.rowcount)
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def renew_dispatch_delivery(
        self,
        *,
        run_id: str,
        lease_token: str,
        lease_seconds: float = 60.0,
    ) -> bool:
        run_uuid = parse_uuid(run_id)
        if run_uuid is None:
            return False
        now = datetime.utcnow()
        session = await self._session()
        try:
            result = await session.execute(
                update(ConversationDispatchOutbox)
                .where(
                    ConversationDispatchOutbox.run_id == run_uuid,
                    ConversationDispatchOutbox.status == "claimed",
                    ConversationDispatchOutbox.lease_token == lease_token,
                )
                .values(
                    lease_expires_at=now
                    + timedelta(seconds=max(1.0, lease_seconds)),
                    updated_at=now,
                )
            )
            await session.commit()
            return bool(result.rowcount)
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def release_dispatch_delivery(
        self,
        *,
        run_id: str,
        lease_token: str,
    ) -> bool:
        run_uuid = parse_uuid(run_id)
        if run_uuid is None:
            return False
        session = await self._session()
        try:
            result = await session.execute(
                update(ConversationDispatchOutbox)
                .where(
                    ConversationDispatchOutbox.run_id == run_uuid,
                    ConversationDispatchOutbox.status == "claimed",
                    ConversationDispatchOutbox.lease_token == lease_token,
                )
                .values(
                    status="pending",
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    updated_at=datetime.utcnow(),
                )
            )
            await session.commit()
            return bool(result.rowcount)
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def attach_dispatch_message(
        self,
        *,
        run_id: str,
        message_id: str,
    ) -> Dict[str, Any] | None:
        run_uuid = parse_uuid(run_id)
        message_uuid = parse_uuid(message_id)
        if run_uuid is None or message_uuid is None:
            return None
        session = await self._session()
        try:
            await session.execute(
                update(AgentRun)
                .where(AgentRun.id == run_uuid)
                .values(
                    trigger_message_id=message_uuid,
                    updated_at=datetime.utcnow(),
                )
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
        return await self.get_run(str(run_uuid))

    async def update_runtime_route(
        self,
        run_id: str | None,
        *,
        provider: str | None,
        model: str | None,
        route_source: str | None = None,
        reasoning_effort: str | None = None,
    ) -> Dict[str, Any] | None:
        """Correct the route used by a live run without changing its lifecycle.

        The initial ``run.started`` event is emitted before a session-aware
        client can be materialized, so its provider/model may describe the
        process-wide client rather than the target that actually generated the
        response.  This small metadata-only update deliberately does not call
        ``_append_event``: route correction is not a second lifecycle event.
        """

        run_uuid = parse_uuid(run_id)
        if run_uuid is None:
            return None

        normalized_provider = str(provider or "").strip()
        normalized_model = str(model or "").strip()
        normalized_route_source = str(route_source or "").strip()
        normalized_effort = str(reasoning_effort or "").strip()

        session = await self._session()
        try:
            run = await session.get(AgentRun, run_uuid)
            if run is None:
                return None

            if normalized_provider:
                run.provider = normalized_provider
            if normalized_model:
                run.model = normalized_model

            metadata = (
                dict(run.run_metadata)
                if isinstance(run.run_metadata, dict)
                else {}
            )
            if normalized_route_source:
                metadata["route_source"] = normalized_route_source
            if normalized_effort:
                metadata["reasoning_effort"] = normalized_effort
            run.run_metadata = _safe_audit_metadata(
                metadata,
                run_type=run.run_type,
            )
            run.updated_at = datetime.utcnow()

            await session.commit()
            await session.refresh(run)
            return run.to_dict()
        except Exception:
            await session.rollback()
            logger.warning(
                "Failed to update runtime route for agent run: %s",
                run_id,
                exc_info=True,
            )
            return None
        finally:
            await session.close()

    async def wait_for_dispatch_result(
        self,
        *,
        session_id: str,
        user_id: str,
        client_message_id: str,
        timeout_seconds: float = 10.0,
    ) -> Dict[str, Any] | None:
        """並行winnerがmessage IDを紐付けるまで短時間待ち、同じ結果を返す。"""
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        latest: Dict[str, Any] | None = None
        while True:
            latest = await self.get_dispatch_run(
                session_id=session_id,
                user_id=user_id,
                client_message_id=client_message_id,
            )
            if latest is None or latest.get("trigger_message_id"):
                return latest
            if time.monotonic() >= deadline:
                return latest
            await asyncio.sleep(0.02)

    async def get_run(
        self,
        run_id: str,
        *,
        include_events: bool = False,
        include_tool_calls: bool = False,
        include_edges: bool = False,
        include_timeline: bool = False,
    ) -> Dict[str, Any] | None:
        run_uuid = parse_uuid(run_id)
        if run_uuid is None:
            return None

        session = await self._session()
        try:
            options = []
            if include_events or include_timeline:
                options.append(selectinload(AgentRun.events))
            if include_tool_calls or include_timeline:
                options.append(selectinload(AgentRun.tool_calls))
            if include_edges:
                options.extend(
                    [
                        selectinload(AgentRun.child_edges),
                        selectinload(AgentRun.parent_edges),
                    ]
                )
            stmt = select(AgentRun).where(AgentRun.id == run_uuid)
            if options:
                stmt = stmt.options(*options)
            result = await session.execute(stmt)
            run = result.scalars().first()
            if not run:
                return None
            payload = _sanitize_serialized_agent_run(
                run.to_dict(
                    include_events=include_events,
                    include_tool_calls=include_tool_calls,
                    include_edges=include_edges,
                )
            )
            if include_timeline:
                resource_mutations = build_agent_resource_mutations(
                    run.tool_calls or []
                )
                payload["resource_mutations"] = [
                    sanitize_cloud_advisor_audit_value(item)
                    if _is_cloud_advisor_run_type(run.run_type)
                    else _redact_sensitive_tool_data(item)
                    for item in resource_mutations
                    if isinstance(item, dict)
                ]
                timeline = build_agent_run_timeline(run)
                if _is_cloud_advisor_run_type(run.run_type):
                    payload["timeline"] = [
                        sanitize_cloud_advisor_audit_value(item)
                        for item in timeline
                        if isinstance(item, dict)
                    ]
                else:
                    payload["timeline"] = [
                        _redact_sensitive_tool_data(item)
                        for item in timeline
                        if isinstance(item, dict)
                    ]
            elif include_tool_calls:
                resource_mutations = build_agent_resource_mutations(
                    run.tool_calls or []
                )
                payload["resource_mutations"] = [
                    sanitize_cloud_advisor_audit_value(item)
                    if _is_cloud_advisor_run_type(run.run_type)
                    else _redact_sensitive_tool_data(item)
                    for item in resource_mutations
                    if isinstance(item, dict)
                ]
            return payload
        finally:
            await session.close()

    async def list_runs(
        self,
        *,
        session_id: str | None = None,
        project_id: str | None = None,
        agent_id: str | None = None,
        task_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[Dict[str, Any]]:
        safe_limit = min(max(int(limit or 50), 1), 200)
        stmt = select(AgentRun)
        filters = []
        session_uuid = parse_uuid(session_id)
        project_uuid = parse_uuid(project_id)
        agent_uuid = parse_uuid(agent_id)
        task_uuid = parse_uuid(task_id)
        if session_id and session_uuid is None:
            return []
        if project_id and project_uuid is None:
            return []
        if agent_id and agent_uuid is None:
            return []
        if task_id and task_uuid is None:
            return []
        if session_uuid:
            filters.append(AgentRun.session_id == session_uuid)
        if project_uuid:
            filters.append(AgentRun.project_id == project_uuid)
        if agent_uuid:
            filters.append(AgentRun.agent_id == agent_uuid)
        if task_uuid:
            filters.append(AgentRun.task_id == task_uuid)
        if status:
            filters.append(AgentRun.status == str(status))
        if filters:
            stmt = stmt.where(*filters)
        stmt = stmt.order_by(desc(AgentRun.created_at)).limit(safe_limit)

        session = await self._session()
        try:
            result = await session.execute(stmt)
            return [
                _sanitize_serialized_agent_run(run.to_dict())
                for run in result.scalars().all()
            ]
        finally:
            await session.close()

    @staticmethod
    def activity_run_projection(run: Mapping[str, Any]) -> dict[str, Any]:
        """Project one run without conversation/prompt/event bodies."""

        fields = (
            "id",
            "status",
            "run_type",
            "session_id",
            "project_id",
            "task_id",
            "work_item_id",
            "agent_id",
            "agent_revision_id",
            "created_at",
            "updated_at",
            "started_at",
            "ended_at",
            "error",
        )
        projected = {key: run.get(key) for key in fields if key in run}
        for key in ("created_at", "updated_at", "started_at", "ended_at"):
            if key in projected:
                projected[key] = _activity_timestamp(projected[key])
        manifest = run.get("execution_manifest") or run.get("resolved_execution_manifest")
        if isinstance(manifest, Mapping):
            projected_manifest = {
                key: manifest.get(key)
                for key in (
                    "schema_version",
                    "agent_id",
                    "agent_revision_id",
                    "agent_revision_version",
                    "team_id",
                    "execution_profile_id",
                    "source",
                    "run_scope_hash",
                    "authority_hash",
                    "route_snapshot",
                )
                if manifest.get(key) not in (None, "")
            }
            if projected_manifest:
                projected["execution_manifest"] = projected_manifest
        result = run.get("result")
        if isinstance(result, Mapping):
            refs: list[dict[str, Any]] = []
            for key in ("resource_refs", "resources", "artifacts", "artifact_refs", "result_refs"):
                values = result.get(key)
                if isinstance(values, Mapping):
                    values = [values]
                if not isinstance(values, (list, tuple)):
                    continue
                for item in list(values)[:32]:
                    if isinstance(item, Mapping):
                        ref = {}
                        for name in (
                            "id", "resource_id", "artifact_id", "ref_id",
                            "kind", "type", "hash", "resource_hash",
                            "artifact_hash", "ref_hash",
                        ):
                            if item.get(name) in (None, ""):
                                continue
                            text_value = str(item[name]).strip()
                            if (
                                not text_value
                                or len(text_value) > 255
                                or "://" in text_value
                                or "\\" in text_value
                                or text_value.startswith(("/", "~"))
                            ):
                                continue
                            ref[name] = text_value
                        if ref:
                            refs.append(ref)
                if len(refs) >= 64:
                    break
            if refs:
                projected["resource_refs"] = refs[:64]
        return projected

    async def list_agent_activity(
        self,
        agent_id: str,
        *,
        user_id: str | None = None,
        user_role: str | None = None,
        session_acl: Any | None = None,
        project_acl: Any | None = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """Return a bounded, ACL-filtered Agent activity projection.

        This helper is intended for Agent memory/tool context.  It deliberately
        excludes objective text, event text, transcripts, and tool arguments;
        only identity, lifecycle, scope and safe resource references are
        projected.  ``session_acl``/``project_acl`` are trusted server-side
        predicates and are applied per row, so an Agent grant never becomes a
        private conversation grant.
        """

        agent_uuid = parse_uuid(agent_id)
        if agent_uuid is None:
            return {"agent_id": str(agent_id), "runs": [], "work_items": [], "resource_refs": []}
        safe_limit = min(max(int(limit or 50), 1), 200)

        async def _allowed(predicate: Any, value: str, *, label: str) -> bool:
            if predicate is None:
                return True
            try:
                result = predicate(value, user_id=user_id, user_role=user_role)
            except TypeError:
                try:
                    result = predicate(value, user_id)
                except TypeError:
                    result = predicate(value)
            result = await result if inspect.isawaitable(result) else result
            return bool(result)

        def _safe_resource_refs(value: Any) -> list[dict[str, Any]]:
            refs: list[dict[str, Any]] = []
            if not isinstance(value, Mapping):
                return refs
            for key in ("resource_refs", "resources", "artifacts", "artifact_refs", "result_refs"):
                candidate = value.get(key)
                if isinstance(candidate, Mapping):
                    candidate = [candidate]
                if not isinstance(candidate, (list, tuple)):
                    continue
                for item in list(candidate)[:32]:
                    if not isinstance(item, Mapping):
                        continue
                    projected = {}
                    for name in (
                        "id", "resource_id", "artifact_id", "ref_id",
                        "kind", "type", "hash", "resource_hash",
                        "artifact_hash", "ref_hash",
                    ):
                        if item.get(name) in (None, ""):
                            continue
                        text_value = str(item[name]).strip()
                        if (
                            not text_value
                            or len(text_value) > 255
                            or "://" in text_value
                            or "\\" in text_value
                            or text_value.startswith(("/", "~"))
                        ):
                            continue
                        projected[name] = text_value
                    if projected:
                        refs.append(projected)
                if len(refs) >= 64:
                    break
            return refs[:64]

        def _run_projection(run: Mapping[str, Any]) -> dict[str, Any]:
            projected = self.activity_run_projection(run)
            # Keep the helper's local bounded extractor for legacy result
            # shapes that the public projection intentionally treats as empty.
            if "resource_refs" not in projected:
                refs = _safe_resource_refs(run.get("result"))
                if refs:
                    projected["resource_refs"] = refs
            return projected

        db_session = await self._session()
        try:
            now = datetime.utcnow()
            agent = await db_session.get(Agent, agent_uuid)
            if agent is None or str(getattr(agent, "state", "")) != "active":
                return {
                    "agent_id": str(agent_uuid),
                    "runs": [],
                    "work_items": [],
                    "resource_refs": [],
                }
            run_result = await db_session.execute(
                select(AgentRun)
                .where(AgentRun.agent_id == agent_uuid)
                .order_by(desc(AgentRun.created_at))
                .limit(safe_limit)
            )
            runs: list[dict[str, Any]] = []
            refs: list[dict[str, Any]] = []
            for run in run_result.scalars().all():
                session_value = str(run.session_id) if run.session_id else None
                if session_value and user_id is not None and session_acl is None:
                    continue
                if session_value and not await _allowed(session_acl, session_value, label="session"):
                    continue
                if run.project_id:
                    if user_id is not None and project_acl is None:
                        continue
                    grant = await db_session.scalar(
                        select(AgentProjectGrant).where(
                            AgentProjectGrant.agent_id == agent_uuid,
                            AgentProjectGrant.project_id == run.project_id,
                            AgentProjectGrant.state == "active",
                        )
                    )
                    if grant is None or (
                        grant.active_from is not None and grant.active_from > now
                    ) or (
                        grant.active_until is not None and grant.active_until < now
                    ) or not bool((grant.permissions or {}).get("read")):
                        continue
                    if not await _allowed(project_acl, str(run.project_id), label="project"):
                        continue
                projected = _run_projection(_sanitize_serialized_agent_run(run.to_dict()))
                runs.append(projected)
                refs.extend(projected.get("resource_refs") or [])

            work_result = await db_session.execute(
                select(AgentWorkItem)
                .where(AgentWorkItem.assigned_agent_id == agent_uuid)
                .order_by(desc(AgentWorkItem.updated_at))
                .limit(safe_limit)
            )
            work_items: list[dict[str, Any]] = []
            for item in work_result.scalars().all():
                session_value = getattr(item, "conversation_session_id", None)
                if session_value is None:
                    metadata = getattr(item, "metadata_json", None)
                    session_value = metadata.get("conversation_session_id") if isinstance(metadata, Mapping) else None
                if session_value and user_id is not None and session_acl is None:
                    continue
                if session_value and not await _allowed(session_acl, str(session_value), label="session"):
                    continue
                if item.project_id:
                    if user_id is not None and project_acl is None:
                        continue
                    grant = await db_session.scalar(
                        select(AgentProjectGrant).where(
                            AgentProjectGrant.agent_id == agent_uuid,
                            AgentProjectGrant.project_id == item.project_id,
                            AgentProjectGrant.state == "active",
                        )
                    )
                    if grant is None or (
                        grant.active_from is not None and grant.active_from > now
                    ) or (
                        grant.active_until is not None and grant.active_until < now
                    ) or not bool((grant.permissions or {}).get("read")):
                        continue
                    if not await _allowed(project_acl, str(item.project_id), label="project"):
                        continue
                safe_item = item.to_safe_dict()
                safe_item.pop("metadata", None)
                for key in (
                    "created_at",
                    "updated_at",
                    "started_at",
                    "completed_at",
                    "cancelled_at",
                    "heartbeat_at",
                    "claimed_at",
                    "next_attempt_at",
                ):
                    if key in safe_item:
                        safe_item[key] = _activity_timestamp(safe_item[key])
                safe_item.pop("result_summary", None)
                result_projection = safe_item.pop("result_summary_json", None)
                item_refs = _safe_resource_refs(result_projection)
                if item_refs:
                    safe_item["resource_refs"] = item_refs
                safe_item["conversation_session_id"] = str(session_value) if session_value else None
                work_items.append(safe_item)
                refs.extend(item_refs)
            return {
                "agent_id": str(agent_uuid),
                "runs": runs[:safe_limit],
                "work_items": work_items[:safe_limit],
                "resource_refs": refs[:64],
            }
        finally:
            await db_session.close()

    async def record_event(
        self,
        run_id: str | None,
        event_type: str,
        *,
        status: str | None = None,
        message: str | None = None,
        payload: Dict[str, Any] | None = None,
    ) -> Dict[str, Any] | None:
        run_uuid = parse_uuid(run_id)
        if run_uuid is None:
            return None

        session = await self._session()
        try:
            # Lock before checking terminal state. Without this, concurrent
            # complete/fail calls can both observe a queued run and overwrite
            # the first terminal outcome after the event-sequence lock.
            try:
                locked = await session.execute(
                    select(AgentRun)
                    .where(AgentRun.id == run_uuid)
                    .with_for_update()
                )
                run = locked.scalars().first()
            except Exception:
                run = await session.get(AgentRun, run_uuid)
            if not run:
                return None
            raw_payload = payload or {}
            if event_type in {"stream.tool_start", "stream.tool_end"}:
                raw_payload = sanitize_tool_audit_payload(raw_payload)
            safe_payload = _sanitize_context_manifest_fields(
                _redact_sensitive_tool_data(raw_payload)
            )
            if (
                _clean_tool_name(safe_payload.get("tool_name"))
                .casefold()
                == CLOUD_ADVISOR_TOOL_NAME
            ):
                safe_payload = sanitize_cloud_advisor_audit_value(safe_payload)
            elif _is_cloud_advisor_run_type(run.run_type):
                safe_payload = sanitize_cloud_advisor_audit_value(safe_payload)
            safe_message = (
                _sanitize_cloud_advisor_event_message(event_type, message)
                if _is_cloud_advisor_run_type(run.run_type)
                else (
                    sanitize_durable_error_text(message)
                    if event_type in {"run.failed", "run.cancelled"}
                    else message
                )
            )
            event = await self._append_event(
                session,
                run,
                event_type,
                status=status,
                message=safe_message,
                payload=safe_payload,
            )
            usage = _normalized_agent_run_usage(safe_payload.get("usage"))
            usage_key = str(safe_payload.get("usage_key") or "").strip()
            if usage is not None and (
                not usage_key
                or usage_key
                not in set(
                    str(key)
                    for key in (run.run_metadata or {}).get(
                        "_usage_event_keys", []
                    )
                )
            ):
                metadata = dict(run.run_metadata or {})
                metadata["usage"] = _merge_agent_run_usage(
                    metadata.get("usage"),
                    usage,
                )
                if usage_key:
                    usage_keys = [
                        str(key)
                        for key in metadata.get("_usage_event_keys", [])
                        if str(key).strip()
                    ]
                    usage_keys.append(usage_key)
                    metadata["_usage_event_keys"] = usage_keys[-256:]
                run.run_metadata = _safe_audit_metadata(
                    metadata,
                    run_type=run.run_type,
                )
            # Interaction requests/resolutions use the existing AgentRun
            # metadata as the restart-visible pending marker.  Resolution and
            # terminal events clear it idempotently; no in-memory Future is
            # persisted or reconstructed.
            if isinstance(safe_payload, dict):
                pending_id = str(
                    safe_payload.get("pending_interaction_id")
                    or ""
                ).strip()
                if pending_id or event_type in {
                    "interaction.resolution",
                    "interaction.resolved",
                    "interaction.cancelled",
                    "interaction.timeout",
                }:
                    metadata = dict(run.run_metadata or {})
                    if pending_id and event_type in {
                        "interaction.requested",
                        "interaction.request",
                        "plan.requested",
                    }:
                        metadata["pending_interaction_id"] = pending_id
                        metadata["pending_interaction_kind"] = str(
                            safe_payload.get("interaction_kind") or ""
                        )
                        metadata["pending_interaction_revision"] = int(
                            safe_payload.get("revision") or 0
                        )
                    elif event_type in {
                        "interaction.resolution",
                        "interaction.resolved",
                        "interaction.cancelled",
                        "interaction.timeout",
                    }:
                        current_pending = str(
                            metadata.get("pending_interaction_id") or ""
                        ).strip()
                        # Do not clear a newer request when a delayed response
                        # for an older interaction arrives.
                        if not pending_id or not current_pending or pending_id == current_pending:
                            metadata.pop("pending_interaction_id", None)
                            metadata.pop("pending_interaction_kind", None)
                            metadata.pop("pending_interaction_revision", None)
                    run.run_metadata = _safe_audit_metadata(
                        metadata,
                        run_type=run.run_type,
                    )
            await session.commit()
            return event.to_dict()
        except Exception:
            await session.rollback()
            logger.exception("Failed to record agent run event: %s", run_id)
            return None
        finally:
            await session.close()

    async def mark_running(
        self,
        run_id: str | None,
        *,
        message: str | None = None,
        metadata: Dict[str, Any] | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> Dict[str, Any] | None:
        return await self._set_status(
            run_id,
            "running",
            "run.started",
            message=message or "Agent run started",
            metadata=metadata,
            provider=provider,
            model=model,
            started=True,
        )

    async def set_pending_interaction(
        self,
        run_id: str | None,
        pending_interaction_id: str | None,
        *,
        kind: str | None = None,
        revision: int | None = None,
    ) -> Dict[str, Any] | None:
        """Set/clear the restart-visible pending interaction marker.

        This is metadata-only; the interaction lifecycle itself is appended by
        :meth:`record_event`, so callers can use this helper when they need to
        mark a request before a provider/websocket callback is available.
        """

        run_uuid = parse_uuid(run_id)
        if run_uuid is None:
            return None
        session = await self._session()
        try:
            run = await session.get(AgentRun, run_uuid)
            if run is None:
                return None
            metadata = dict(run.run_metadata or {})
            pending = str(pending_interaction_id or "").strip()
            if pending:
                metadata["pending_interaction_id"] = pending
                if kind is not None:
                    metadata["pending_interaction_kind"] = str(kind)
                if revision is not None:
                    metadata["pending_interaction_revision"] = max(0, int(revision))
            else:
                metadata.pop("pending_interaction_id", None)
                metadata.pop("pending_interaction_kind", None)
                metadata.pop("pending_interaction_revision", None)
            run.run_metadata = _safe_audit_metadata(
                metadata,
                run_type=run.run_type,
            )
            run.updated_at = datetime.utcnow()
            await session.commit()
            await session.refresh(run)
            return run.to_dict()
        except Exception:
            await session.rollback()
            logger.warning("Failed to update pending interaction marker: %s", run_id, exc_info=True)
            return None
        finally:
            await session.close()

    async def _mark_restart_uncertain_work_in_session(
        self,
        session: Any,
        run: AgentRun,
        *,
        safe_error_code: str,
        reason: str,
    ) -> str:
        """Fence one stale Run's Work without making a blind retry eligible."""

        work_id = parse_uuid(getattr(run, "work_item_id", None))
        run_id = parse_uuid(getattr(run, "id", None))
        if work_id is None or run_id is None:
            return "not_applicable"
        work = await session.get(AgentWorkItem, work_id)
        if work is None:
            return "not_applicable"
        if str(getattr(work, "state", "") or "") == "uncertain":
            return "fenced"
        if str(getattr(work, "state", "") or "") not in {"pending", "claimed", "running"}:
            return "not_applicable"
        now = datetime.utcnow()
        if (
            getattr(work, "lease_owner", None)
            and getattr(work, "lease_token", None)
            and getattr(work, "lease_expires_at", None) is not None
            and work.lease_expires_at > now
        ):
            # A different process still owns a live provider/Work lease. Do
            # not fence or terminalize it from this startup pass.
            return "not_applicable"
        try:
            # ``session.get`` can return an identity-map snapshot that was
            # pending when another process claimed the dispatch.  Refresh
            # the row under a lock before deciding whether this Run may be
            # fenced.  The UPDATE below remains a CAS for SQLite and for
            # claims that race after this read.
            outbox = (
                (
                    await session.execute(
                        select(ConversationDispatchOutbox)
                        .where(ConversationDispatchOutbox.run_id == run_id)
                        .execution_options(populate_existing=True)
                        .with_for_update()
                    )
                )
                .scalars()
                .first()
            )
        except OperationalError as exc:
            if "conversation_dispatch_outbox" in str(exc).casefold():
                outbox = None
            else:
                raise
        outbox_fence: dict[str, Any] | None = None
        if outbox is not None:
            outbox_status = str(getattr(outbox, "status", "") or "").casefold()
            outbox_payload = (
                outbox.payload if isinstance(outbox.payload, Mapping) else {}
            )
            payload_run_id = parse_uuid(outbox_payload.get("agent_run_id"))
            if (
                parse_uuid(getattr(outbox, "session_id", None))
                != parse_uuid(getattr(run, "session_id", None))
                or str(getattr(outbox, "user_id", "") or "")
                != str(getattr(run, "user_id", "") or "")
                or (
                    outbox_payload.get("agent_run_id") not in (None, "")
                    and payload_run_id != run_id
                )
            ):
                # An outbox row bound to another session/user/Run is not a
                # restart authority for this Work.  The caller must leave a
                # linked Run untouched rather than terminalizing it.
                return "not_applicable"
        if (
            outbox is not None
            and str(getattr(outbox, "status", "") or "").casefold() == "claimed"
            and getattr(outbox, "lease_owner", None)
            and getattr(outbox, "lease_token", None)
            and getattr(outbox, "lease_expires_at", None) is not None
            and outbox.lease_expires_at > now
        ):
            # The dispatch consumer has a live lease even when the common Work
            # projection has not refreshed its heartbeat yet.
            return "not_applicable"
        if outbox is not None:
            outbox_status = str(getattr(outbox, "status", "") or "").casefold()
            if outbox_status in {"pending", "claimed"}:
                outbox_fence = {
                    "status": outbox_status,
                    "lease_owner": getattr(outbox, "lease_owner", None),
                    "lease_token": getattr(outbox, "lease_token", None),
                    "lease_expires_at": getattr(outbox, "lease_expires_at", None),
                    "updated_at": getattr(outbox, "updated_at", None),
                }
            elif outbox_status not in {"delivered"}:
                # Cancelled/dead-letter/unknown rows cannot be claimed by the
                # dispatch worker and are not proof that this linked Work may
                # be terminalized during restart.
                return "not_applicable"
        if (
            parse_uuid(getattr(work, "active_agent_run_id", None)) != run_id
            or str(getattr(work, "source_id", "") or "") != str(run.id)
            or parse_uuid(getattr(work, "conversation_session_id", None))
            != parse_uuid(getattr(run, "session_id", None))
            or parse_uuid(getattr(work, "assigned_agent_id", None))
            != parse_uuid(getattr(run, "agent_id", None))
            or str(getattr(work, "source_revision", "") or "")
            != str(getattr(run, "agent_revision_id", "") or "")
            or parse_uuid(getattr(work, "agent_revision_id", None))
            != parse_uuid(getattr(run, "agent_revision_id", None))
        ):
            return "not_applicable"
        effective_error_code = safe_error_code
        run_metadata = run.run_metadata if isinstance(run.run_metadata, Mapping) else {}
        marker = run_metadata.get("agent_dialogue")
        if isinstance(marker, Mapping) and marker.get("dialogue_id") not in (None, ""):
            dialogue_uuid = parse_uuid(marker.get("dialogue_id"))
            delivery_uuid = parse_uuid(marker.get("delivery_id"))
            dialogue = await session.get(AgentDialogue, dialogue_uuid) if dialogue_uuid else None
            delivery = await session.get(AgentDialogueDelivery, delivery_uuid) if delivery_uuid else None
            # A delivery id makes this Run a receiver attempt.  Only that
            # typed receiver contract may produce a fence conflict. Root
            # Human/source Runs intentionally carry Dialogue ancestry without
            # a receiver Delivery and use their own restart contract.
            if delivery_uuid is not None and (
                dialogue is None
                or delivery is None
                or parse_uuid(delivery.receiver_run_id) != run_id
                or parse_uuid(delivery.receiver_work_item_id) != work.id
                or int(delivery.expected_control_revision or 0)
                != int(dialogue.control_revision or 0)
            ):
                # The provider attempt is still fenced as uncertain, but a
                # stale control/delivery identity must never be treated as a
                # current Dialogue callback or replay authority.
                effective_error_code = "agent_run_restart_fence_conflict"
        previous_state = str(work.state or "")
        now = datetime.utcnow()
        nested = await session.begin_nested()
        try:
            if outbox_fence is not None:
                outbox_update = await session.execute(
                    update(ConversationDispatchOutbox)
                    .where(
                        ConversationDispatchOutbox.run_id == run_id,
                        ConversationDispatchOutbox.status
                        == outbox_fence["status"],
                        ConversationDispatchOutbox.lease_owner
                        == outbox_fence["lease_owner"],
                        ConversationDispatchOutbox.lease_token
                        == outbox_fence["lease_token"],
                        ConversationDispatchOutbox.lease_expires_at
                        == outbox_fence["lease_expires_at"],
                        ConversationDispatchOutbox.updated_at
                        == outbox_fence["updated_at"],
                    )
                    .values(
                        status="cancelled",
                        lease_owner=None,
                        lease_token=None,
                        lease_expires_at=None,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                if int(getattr(outbox_update, "rowcount", 0) or 0) != 1:
                    # A dispatch claim won after the fresh read. Roll back
                    # the savepoint so neither the Work nor the Run can be
                    # terminalized against that new owner.
                    await nested.rollback()
                    return "cas_lost"

            fence = await session.execute(
                update(AgentWorkItem)
                .where(
                    AgentWorkItem.id == work.id,
                    AgentWorkItem.state == previous_state,
                    AgentWorkItem.active_agent_run_id == run.id,
                    AgentWorkItem.lease_owner == work.lease_owner,
                    AgentWorkItem.lease_token == work.lease_token,
                    AgentWorkItem.lease_expires_at == work.lease_expires_at,
                    AgentWorkItem.updated_at == work.updated_at,
                )
                .values(
                    state="uncertain",
                    outcome_classification="uncertain",
                    safe_error_code=effective_error_code,
                    escalation_reason=sanitize_durable_error_text(reason),
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    heartbeat_at=now,
                    updated_at=now,
                    completed_at=now,
                    active_agent_run_id=None,
                    not_before=None,
                    next_attempt_at=None,
                )
                .execution_options(synchronize_session=False)
            )
            if int(getattr(fence, "rowcount", 0) or 0) != 1:
                # Another process claimed/released this Work between the
                # snapshot and the fenced UPDATE. Never terminalize the
                # stale Run after a lost CAS; the new owner is authoritative.
                await nested.rollback()
                return "cas_lost"
            work.state = "uncertain"
            work.outcome_classification = "uncertain"
            work.safe_error_code = effective_error_code
            work.escalation_reason = sanitize_durable_error_text(reason)
            work.lease_owner = None
            work.lease_token = None
            work.lease_expires_at = None
            work.heartbeat_at = now
            work.updated_at = now
            work.completed_at = now
            work.active_agent_run_id = None
            work.not_before = None
            work.next_attempt_at = None
            if outbox is not None and outbox_fence is not None:
                outbox.status = "cancelled"
                outbox.lease_owner = None
                outbox.lease_token = None
                outbox.lease_expires_at = None
                outbox.updated_at = now
            work_sequence = await session.scalar(
                select(func.max(AgentWorkEvent.sequence)).where(
                    AgentWorkEvent.work_item_id == work.id
                )
            )
            session.add(
                AgentWorkEvent(
                    id=uuid.uuid4(),
                    work_item_id=work.id,
                    sequence=int(work_sequence or 0) + 1,
                    event_type="work.uncertain",
                    from_state=previous_state,
                    to_state="uncertain",
                    actor_kind="service",
                    actor_id="aoitalk.system",
                    actor_service_key="aoitalk.system",
                    agent_run_id=run.id,
                    safe_error_code=effective_error_code,
                    result_summary=sanitize_durable_error_text(reason),
                    payload_json={
                        "source": "agent_run_restart_reconcile",
                        "recovered_after_restart": True,
                        "run_id": str(run.id),
                    },
                    created_at=now,
                )
            )
            await nested.commit()
        except Exception:
            await nested.rollback()
            raise
        return "fenced"

    async def _materialize_restart_uncertain_root_notice_in_session(
        self,
        session: Any,
        run: AgentRun,
    ) -> Dict[str, Any] | None:
        """Write one service-authored Root notice for an uncertain Worker Run."""

        metadata = run.run_metadata if isinstance(run.run_metadata, Mapping) else {}
        marker = metadata.get("agent_dialogue")
        if not isinstance(marker, Mapping) or str(marker.get("role") or "").casefold() != "worker":
            return None
        dialogue_id = parse_uuid(marker.get("dialogue_id"))
        delivery_id = parse_uuid(marker.get("delivery_id"))
        run_id = parse_uuid(run.id)
        work_id = parse_uuid(run.work_item_id)
        if dialogue_id is None or delivery_id is None or run_id is None or work_id is None:
            return None
        dialogue = (
            await session.execute(
                select(AgentDialogue).where(AgentDialogue.id == dialogue_id).with_for_update()
            )
        ).scalars().first()
        delivery = await session.get(AgentDialogueDelivery, delivery_id)
        work = await session.get(AgentWorkItem, work_id)
        if dialogue is None or delivery is None or work is None:
            return None
        owner_uuid = parse_uuid(dialogue.owner_user_id)
        if (
            str(run.status or "").casefold() != "failed"
            or str(work.state or "").casefold() != "uncertain"
            or parse_uuid(work.active_agent_run_id) is not None
            or parse_uuid(delivery.dialogue_id) != dialogue_id
            or parse_uuid(delivery.receiver_run_id) != run_id
            or parse_uuid(delivery.receiver_work_item_id) != work_id
            or int(delivery.expected_control_revision or 0) != int(dialogue.control_revision or 0)
            or parse_uuid(run.session_id) != parse_uuid(dialogue.worker_session_id)
            or parse_uuid(run.agent_id) != parse_uuid(dialogue.worker_agent_id)
            or parse_uuid(run.agent_revision_id) != parse_uuid(dialogue.worker_agent_revision_id)
            or parse_uuid(work.conversation_session_id) != parse_uuid(run.session_id)
            or parse_uuid(work.assigned_agent_id) != parse_uuid(run.agent_id)
            or parse_uuid(work.agent_revision_id) != parse_uuid(run.agent_revision_id)
            or str(work.source_id or "") != str(run.id)
            or str(work.source_revision or "") != str(run.agent_revision_id)
            or owner_uuid is None
            or parse_uuid(run.user_id) != owner_uuid
        ):
            return None
        notice_code = "agent_dialogue_restart_uncertain"
        notice_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"aoitalk:agent-dialogue-restart-notice:{dialogue_id}:{run_id}:{int(dialogue.control_revision or 0)}",
        )
        root_session_id = parse_uuid(dialogue.root_session_id)
        root_session = await session.get(ConversationSession, root_session_id) if root_session_id else None
        if root_session is None or parse_uuid(root_session.user_id) != owner_uuid:
            return None
        existing = await session.get(ConversationMessage, notice_id)
        if existing is not None:
            existing_metadata = existing.message_metadata if isinstance(existing.message_metadata, Mapping) else {}
            if (
                parse_uuid(existing.session_id) != root_session_id
                or str(existing.sender_type or "").casefold() != "system"
                or str(existing.sender_id or "") != "aoitalk.system"
                or str(existing_metadata.get("safe_error_code") or "").casefold() != notice_code
                or str(existing_metadata.get("agent_run_id") or "") != str(run_id)
            ):
                return None
            return existing.to_dict()
        now = datetime.utcnow()
        notice_metadata = {
            "source": "aoitalk.system",
            "safe_error_code": notice_code,
            "agent_dialogue_id": str(dialogue_id),
            "agent_dialogue_delivery_id": str(delivery_id),
            "agent_run_id": str(run_id),
            "agent_work_item_id": str(work_id),
            "control_revision": int(dialogue.control_revision or 0),
            "provider_started": False,
            "restart_recovery_state": "uncertain",
            "sender_kind": "system",
        }
        notice = ConversationMessage(
            id=notice_id,
            session_id=root_session_id,
            role="assistant",
            content=(
                "Worker Agentの実行状態を再起動後に確定できませんでした。"
                "自動再実行は行っていません。状態を確認してから再開してください。"
            ),
            client_message_id=f"agent-dialogue-restart-notice:{dialogue_id}:{run_id}:{int(dialogue.control_revision or 0)}",
            message_metadata=_jsonable(notice_metadata),
            sender_type="system",
            sender_id="aoitalk.system",
            sender_display_name="AoiTalk System",
            created_at=now,
            updated_at=now,
        )
        session.add(notice)
        if str(dialogue.state or "").casefold() in {"queued", "running", "waiting"}:
            dialogue.state = "blocked"
            dialogue.updated_at = now
        budget_root_id = parse_uuid(getattr(dialogue, "budget_root_dialogue_id", None))
        if budget_root_id is not None:
            try:
                from ..memory.models import AgentDialogueBudgetRoot

                budget_root = await session.get(AgentDialogueBudgetRoot, budget_root_id)
                if budget_root is not None and str(budget_root.state or "").casefold() == "active":
                    budget_root.state = "blocked"
                    budget_root.updated_at = now
            except OperationalError:
                pass
        await session.execute(
            update(ConversationSession)
            .where(ConversationSession.id == root_session_id)
            .values(
                message_count=ConversationSession.message_count + 1,
                last_activity=_monotonic_activity(now),
            )
        )
        await session.flush()
        return notice.to_dict()

    async def reconcile_stale_runs_after_restart(
        self,
        *,
        limit: int = 500,
        reason: str = "Agent run interrupted by process restart",
    ) -> dict[str, Any]:
        """Fail-closed reconciliation for in-flight runs after startup.

        In-memory provider tasks/Futures are intentionally not recreated.  A
        stale Run is marked failed once only after its linked Work and outbox
        pass the same restart fence; a live or concurrently reclaimed owner
        remains untouched.  Open Director edges to terminal children are
        closed in the same transaction.
        """

        safe_limit = min(max(int(limit or 500), 1), 5000)
        session = await self._session()
        reconciled: list[str] = []
        terminalized: list[str] = []
        uncertain_work = 0
        closed_edges = 0
        try:
            result = await session.execute(
                select(AgentRun)
                .where(AgentRun.status == "running")
                .order_by(AgentRun.updated_at)
                .limit(safe_limit)
                .with_for_update()
            )
            now = datetime.utcnow()
            safe_reason = sanitize_durable_error_text(reason)
            for run in result.scalars().all():
                if run.status in RUN_TERMINAL_STATUSES:
                    continue
                run_work = (
                    await session.get(AgentWorkItem, run.work_item_id)
                    if run.work_item_id is not None
                    else None
                )
                if (
                    run_work is not None
                    and getattr(run_work, "lease_owner", None)
                    and getattr(run_work, "lease_token", None)
                    and getattr(run_work, "lease_expires_at", None) is not None
                    and run_work.lease_expires_at > now
                ):
                    # Another process owns this live Work lease; startup
                    # reconciliation must not interfere with it.
                    continue
                try:
                    run_outbox = (
                        (
                            await session.execute(
                                select(ConversationDispatchOutbox)
                                .where(
                                    ConversationDispatchOutbox.run_id == run.id
                                )
                                .execution_options(populate_existing=True)
                                .with_for_update()
                            )
                        )
                        .scalars()
                        .first()
                    )
                except OperationalError as exc:
                    if "conversation_dispatch_outbox" in str(exc).casefold():
                        run_outbox = None
                    else:
                        raise
                if (
                    run_outbox is not None
                    and str(getattr(run_outbox, "status", "") or "").casefold()
                    == "claimed"
                    and getattr(run_outbox, "lease_owner", None)
                    and getattr(run_outbox, "lease_token", None)
                    and getattr(run_outbox, "lease_expires_at", None) is not None
                    and run_outbox.lease_expires_at > now
                ):
                    continue
                fence_result = await self._mark_restart_uncertain_work_in_session(
                    session,
                    run,
                    safe_error_code="agent_run_restart_uncertain",
                    reason=safe_reason,
                )
                if fence_result == "cas_lost":
                    # A concurrent Work claim won the fence race. Leave both
                    # Run and Work untouched; the other process owns them.
                    continue
                if run.work_item_id is not None and fence_result != "fenced":
                    # A linked Work with a live lease, stale identity, or an
                    # ineligible outbox is not this startup pass's authority.
                    # Legacy Runs without a Work ledger keep the historical
                    # terminalization path below.
                    continue
                if fence_result == "fenced":
                    uncertain_work += 1
                metadata = dict(run.run_metadata or {})
                pending_id = metadata.get("pending_interaction_id")
                metadata.pop("pending_interaction_id", None)
                metadata.pop("pending_interaction_kind", None)
                metadata.pop("pending_interaction_revision", None)
                metadata["reconciled_after_restart"] = True
                metadata["restart_recovery_state"] = "uncertain"
                run.run_metadata = _safe_audit_metadata(
                    metadata,
                    run_type=run.run_type,
                )
                reconciled.append(str(run.id))

            # Director child edges are independently durable.  Close any open
            # edge whose child is already terminal, including children that
            # completed before a crash but missed the controller callback.
            try:
                edge_result = await session.execute(
                    select(AgentRunEdge, AgentRun.status)
                    .join(AgentRun, AgentRun.id == AgentRunEdge.child_run_id)
                    .where(
                        AgentRunEdge.status == "open",
                        AgentRun.status.in_(tuple(RUN_TERMINAL_STATUSES)),
                    )
                    .limit(safe_limit)
                )
                for edge, child_status in edge_result.all():
                    edge.status = str(child_status)
                    edge.closed_at = edge.closed_at or now
                    closed_edges += 1
            except OperationalError as exc:
                # Focused legacy fixtures may omit the optional Director edge
                # table.  Run/Work settlement remains authoritative and does
                # not roll back merely because no edge ledger exists.
                if "agent_run_edges" not in str(exc).casefold():
                    raise
            await session.commit()
            # Run terminalization owns usage unknown/release and Dialogue
            # source accounting.  Work remains ``uncertain`` so a later
            # outbox/coordinator pass cannot replay the provider blindly.
            for run_id in reconciled:
                terminal = await self.fail_run(
                    run_id,
                    safe_reason,
                    result={
                        "source": "agent_run_restart_reconcile",
                        "safe_error_code": "agent_run_restart_uncertain",
                        "reconciled_after_restart": True,
                    },
                    metadata={
                        "reconciled_after_restart": True,
                        "restart_recovery_state": "uncertain",
                    },
                )
                if terminal is not None:
                    terminalized.append(run_id)
            return {
                "reconciled_run_ids": reconciled,
                "reconciled": len(reconciled),
                "closed_edges": closed_edges,
                "uncertain_work": uncertain_work,
                "terminalized_run_ids": terminalized,
                "terminalized": len(terminalized),
            }
        except Exception:
            await session.rollback()
            logger.warning("Failed to reconcile stale AgentRuns after restart", exc_info=True)
            return {
                "reconciled_run_ids": [],
                "reconciled": 0,
                "closed_edges": 0,
                "uncertain_work": 0,
                "terminalized_run_ids": [],
                "terminalized": 0,
            }
        finally:
            await session.close()

    # Short alias for startup hooks/callers that prefer the noun first.
    async def reconcile_stale_runs(self, **kwargs: Any) -> dict[str, Any]:
        return await self.reconcile_stale_runs_after_restart(**kwargs)

    async def complete_run(
        self,
        run_id: str | None,
        *,
        result: Dict[str, Any] | None = None,
        message: str | None = None,
        metadata: Dict[str, Any] | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> Dict[str, Any] | None:
        return await self._set_status(
            run_id,
            "succeeded",
            "run.succeeded",
            message=message or "Agent run completed",
            result=result,
            metadata=metadata,
            provider=provider,
            model=model,
            ended=True,
        )

    async def materialize_agent_dialogue_terminal_failure(
        self,
        session: Any,
        dialogue: Any,
        delivery: Any,
        receiver_run: Any,
        receiver_work: Any,
        safe_error_code: str,
    ) -> Dict[str, Any] | None:
        """Materialize one typed Worker failure in the caller transaction."""

        normalized_code = str(safe_error_code or "").strip().casefold()
        if (
            not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", normalized_code)
            or not normalized_code.startswith(
                (
                    "agent_route_",
                    "interactive_admission_",
                    "agent_dialogue_budget_",
                )
            )
        ):
            return None
        if receiver_run is None or receiver_work is None or dialogue is None or delivery is None:
            return None
        metadata = (
            receiver_run.run_metadata
            if isinstance(receiver_run.run_metadata, Mapping)
            else {}
        )
        marker = metadata.get("agent_dialogue")
        result_metadata = (
            receiver_run.result
            if isinstance(receiver_run.result, Mapping)
            else {}
        )
        blocked_authority_code = normalized_code.startswith(
            "interactive_admission_authority_"
        )
        if blocked_authority_code:
            provider_started_values = [
                evidence.get("provider_started")
                for evidence in (metadata, result_metadata)
                if isinstance(evidence, Mapping)
            ]
            if not any(value is False for value in provider_started_values):
                return None
        # ``AgentRun.error`` may contain a provider/setup detail that is safe
        # to retain in the private run row but must never be used as the
        # receipt identity or copied into the worker message.  A separately
        # persisted safe code is authoritative.  If an explicit safe code is
        # present in run evidence, it must agree with the Work code; otherwise
        # reject the receipt rather than allowing a forged/mismatched marker.
        for evidence in (metadata, result_metadata):
            evidence_code = evidence.get("safe_error_code")
            if evidence_code not in (None, ""):
                evidence_code = str(evidence_code).strip().casefold()
                if evidence_code != normalized_code:
                    return None
        marker_code = marker.get("safe_error_code") if isinstance(marker, Mapping) else None
        if marker_code not in (None, "") and str(marker_code).strip().casefold() != normalized_code:
            return None
        run_error = str(receiver_run.error or "").strip().casefold()
        if (
            run_error
            and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", run_error)
            and run_error.startswith(
                (
                    "agent_route_",
                    "interactive_admission_",
                    "agent_dialogue_budget_",
                )
            )
            and run_error != normalized_code
        ):
            return None
        agent_uuid = parse_uuid(receiver_run.agent_id)
        session_uuid = parse_uuid(receiver_run.session_id)
        delivery_dialogue_uuid = parse_uuid(delivery.dialogue_id)
        delivery_receiver_run_uuid = parse_uuid(delivery.receiver_run_id)
        delivery_receiver_session_uuid = parse_uuid(delivery.receiver_session_id)
        delivery_receiver_agent_uuid = parse_uuid(delivery.receiver_agent_id)
        delivery_receiver_revision_uuid = parse_uuid(
            delivery.receiver_agent_revision_id
        )
        delivery_receiver_work_uuid = parse_uuid(delivery.receiver_work_item_id)
        delivery_source_session_uuid = parse_uuid(delivery.source_session_id)
        delivery_source_agent_uuid = parse_uuid(delivery.source_agent_id)
        delivery_source_revision_uuid = parse_uuid(delivery.source_agent_revision_id)
        delivery_source_run_uuid = parse_uuid(delivery.source_run_id)
        delivery_source_work_uuid = parse_uuid(delivery.source_work_item_id)
        delivery_source_message_uuid = parse_uuid(delivery.source_message_id)
        receiver_run_uuid = parse_uuid(receiver_run.id)
        receiver_run_session_uuid = parse_uuid(receiver_run.session_id)
        receiver_run_agent_uuid = parse_uuid(receiver_run.agent_id)
        receiver_work_id = parse_uuid(receiver_work.id)
        receiver_run_work_id = parse_uuid(receiver_run.work_item_id)
        receiver_work_agent_uuid = parse_uuid(receiver_work.assigned_agent_id)
        receiver_work_revision_uuid = parse_uuid(receiver_work.agent_revision_id)
        receiver_run_revision_uuid = parse_uuid(receiver_run.agent_revision_id)
        receiver_work_active_run_uuid = parse_uuid(receiver_work.active_agent_run_id)
        receiver_work_state = str(receiver_work.state or "").casefold()
        blocked_authority_work = (
            blocked_authority_code
            and receiver_work_state == "blocked"
            and str(getattr(receiver_work, "outcome_classification", "") or "").casefold()
            == "blocked"
        )
        if (
            str(receiver_run.status or "").casefold() != "failed"
            or not isinstance(marker, Mapping)
            or str(marker.get("role") or "").casefold() != "worker"
            or parse_uuid(marker.get("dialogue_id")) != parse_uuid(dialogue.id)
            or parse_uuid(marker.get("delivery_id")) != parse_uuid(delivery.id)
            or delivery_dialogue_uuid != parse_uuid(dialogue.id)
            or delivery_receiver_run_uuid != receiver_run_uuid
            or delivery_receiver_session_uuid != receiver_run_session_uuid
            or delivery_receiver_agent_uuid != receiver_run_agent_uuid
            or delivery_receiver_revision_uuid != receiver_run_revision_uuid
            or delivery_receiver_work_uuid not in (None, receiver_work_id)
            or delivery_source_session_uuid != parse_uuid(dialogue.root_session_id)
            or delivery_source_agent_uuid != parse_uuid(dialogue.root_agent_id)
            or delivery_source_revision_uuid != parse_uuid(dialogue.root_agent_revision_id)
            or delivery_source_run_uuid is None
            or delivery_source_work_uuid is None
            or delivery_source_message_uuid is None
            or parse_uuid(dialogue.worker_session_id) != receiver_run_session_uuid
            or parse_uuid(dialogue.worker_agent_id) != receiver_run_agent_uuid
            or receiver_work_id != receiver_run_work_id
            or receiver_work_agent_uuid != receiver_run_agent_uuid
            or receiver_work_revision_uuid != receiver_run_revision_uuid
            or receiver_work_active_run_uuid not in (None, receiver_run_uuid)
            or (
                receiver_work_state != "failed"
                and not blocked_authority_work
            )
            or str(getattr(receiver_work, "safe_error_code", "") or "").strip().casefold()
            != normalized_code
            or str(getattr(receiver_work, "source_type", "") or "").casefold()
            != "chat_turn"
            or str(getattr(receiver_work, "execution_adapter", "") or "").casefold()
            != "interactive_chat"
            or str(getattr(receiver_work, "source_id", "") or "")
            != str(receiver_run.id)
            or parse_uuid(getattr(receiver_work, "conversation_session_id", None))
            != receiver_run_session_uuid
            or agent_uuid is None
            or session_uuid is None
            or receiver_run_uuid is None
            or receiver_run_session_uuid is None
            or receiver_run_agent_uuid is None
        ):
            return None
        owner_uuid = parse_uuid(receiver_run.user_id)
        if owner_uuid is None or owner_uuid != parse_uuid(dialogue.owner_user_id):
            return None
        owner = await self._require_dialogue_human_owner(session, receiver_run.user_id)
        if owner != owner_uuid:
            return None
        receiver_session = await session.get(ConversationSession, receiver_run.session_id)
        if receiver_session is None or parse_uuid(receiver_session.user_id) != owner_uuid:
            return None
        message_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"aoitalk:agent-dialogue-failure:{receiver_run.id}:{normalized_code}",
        )
        existing = await session.get(ConversationMessage, message_id)
        if existing is not None:
            existing_metadata = (
                existing.message_metadata
                if isinstance(existing.message_metadata, Mapping)
                else {}
            )
            if (
                parse_uuid(existing.session_id) != session_uuid
                or str(existing.role or "").casefold() != "assistant"
                or str(existing.sender_type or "").casefold() != "agent"
                or parse_uuid(existing.sender_id) != agent_uuid
                or str(existing_metadata.get("agent_run_id") or "")
                != str(receiver_run.id)
                or str(existing_metadata.get("agent_dialogue_id") or "")
                != str(dialogue.id)
                or str(existing_metadata.get("agent_dialogue_delivery_id") or "")
                != str(delivery.id)
                or str(existing_metadata.get("safe_error_code") or "").casefold()
                != normalized_code
            ):
                return None
            return existing.to_dict()
        message_metadata = {
            "agent_run_id": str(receiver_run.id),
            "agent_dialogue_id": str(dialogue.id),
            "agent_dialogue_delivery_id": str(delivery.id),
            "agent_dialogue": {
                "dialogue_id": str(dialogue.id),
                "delivery_id": str(delivery.id),
                "role": "worker",
                "failure": True,
                "safe_error_code": normalized_code,
            },
            "safe_error_code": normalized_code,
        }
        message = ConversationMessage(
            id=message_id,
            session_id=session_uuid,
            role="assistant",
            content="Worker Agentの実行を開始できませんでした。",
            message_metadata=_jsonable(message_metadata),
            client_message_id=f"agent-dialogue-failure:{receiver_run.id}:{normalized_code}",
            sender_type="agent",
            sender_id=str(agent_uuid),
            sender_display_name="Worker Agent",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        session.add(message)
        await session.flush()
        return message.to_dict()

    async def persist_agent_dialogue_failure_message(
        self,
        run_id: str | None,
        safe_error_code: str,
    ) -> Dict[str, Any] | None:
        """Recover the canonical failure message through one owned transaction."""

        run_uuid = parse_uuid(run_id)
        if run_uuid is None:
            return None
        db_session = await self._session()
        try:
            receiver_run = (
                await db_session.execute(
                    select(AgentRun)
                    .where(AgentRun.id == run_uuid)
                    .with_for_update()
                )
            ).scalars().first()
            if receiver_run is None:
                return None
            metadata = receiver_run.run_metadata if isinstance(receiver_run.run_metadata, Mapping) else {}
            marker = metadata.get("agent_dialogue")
            dialogue_uuid = parse_uuid(marker.get("dialogue_id")) if isinstance(marker, Mapping) else None
            delivery_uuid = parse_uuid(marker.get("delivery_id")) if isinstance(marker, Mapping) else None
            dialogue = await db_session.get(AgentDialogue, dialogue_uuid) if dialogue_uuid else None
            delivery = await db_session.get(AgentDialogueDelivery, delivery_uuid) if delivery_uuid else None
            work = await db_session.get(AgentWorkItem, receiver_run.work_item_id) if receiver_run.work_item_id else None
            result = await self.materialize_agent_dialogue_terminal_failure(
                db_session,
                dialogue,
                delivery,
                receiver_run,
                work,
                safe_error_code,
            )
            await db_session.commit()
            return result
        except IntegrityError:
            await db_session.rollback()
            return None
        except Exception:
            await db_session.rollback()
            raise
        finally:
            await db_session.close()

    async def fail_run(
        self,
        run_id: str | None,
        error: str,
        *,
        result: Dict[str, Any] | None = None,
        status: str = "failed",
        metadata: Dict[str, Any] | None = None,
        provider: str | None = None,
        model: str | None = None,
        preprovider_proof: PreproviderFailureProof | None = None,
    ) -> Dict[str, Any] | None:
        safe_status = (
            status
            if status in {"failed", "cancelled", "awaiting_approval"}
            else "failed"
        )
        event_type = (
            "run.cancelled"
            if safe_status == "cancelled"
            else "run.awaiting_approval"
            if safe_status == "awaiting_approval"
            else "run.failed"
        )
        ended = safe_status in {"failed", "cancelled"}
        return await self._set_status(
            run_id,
            safe_status,
            event_type,
            message=error,
            result=result,
            metadata=metadata,
            provider=provider,
            model=model,
            error=error,
            ended=ended,
            preprovider_proof=preprovider_proof,
        )

    async def cancel_run(
        self,
        run_id: str | None,
        *,
        message: str | None = None,
    ) -> Dict[str, Any] | None:
        return await self.fail_run(
            run_id,
            message or "Agent run cancelled",
            status="cancelled",
        )

    async def finalize_cancelled_chat_turn(
        self,
        run_id: str | None,
        *,
        message: str = "Conversation generation stopped by user",
    ) -> Dict[str, Any] | None:
        """Persist the partial assistant output and cancelled run exactly once."""

        run_uuid = parse_uuid(run_id)
        if run_uuid is None:
            return None

        session = await self._session()
        try:
            # Discover only the binding: loading an ORM run before waiting for
            # the conversation lock would leave its status/result stale in the
            # identity map when a concurrent terminal transition wins.
            session_uuid = await session.scalar(
                select(AgentRun.session_id).where(
                    AgentRun.id == run_uuid,
                    AgentRun.run_type == "chat_turn",
                )
            )
            if session_uuid is None:
                return None

            conversation_result = await session.execute(
                select(ConversationSession)
                .where(
                    ConversationSession.id == session_uuid,
                    ConversationSession.deleted_at.is_(None),
                )
                .with_for_update()
            )
            conversation = conversation_result.scalars().first()
            if conversation is None:
                return None

            run_result = await session.execute(
                select(AgentRun)
                .where(AgentRun.id == run_uuid)
                .with_for_update()
            )
            run = run_result.scalars().first()
            if run is None:
                return None

            if run.status in {"succeeded", "failed"}:
                return None

            current_result = dict(run.result or {})
            existing_message_id = parse_uuid(
                current_result.get("assistant_message_id")
            )
            if existing_message_id is not None:
                existing_message = await session.get(
                    ConversationMessage,
                    existing_message_id,
                )
                if existing_message is not None:
                    if run.status == "cancelled":
                        existing_metadata = dict(
                            existing_message.message_metadata or {}
                        )
                        existing_metadata.update(
                            {
                                "agent_run_id": str(run.id),
                                "generation_status": "cancelled",
                                "partial": True,
                                "finish_reason": "user_stop",
                            }
                        )
                        existing_message.message_metadata = _jsonable(
                            existing_metadata
                        )
                        await session.commit()
                        await session.refresh(existing_message)
                    return existing_message.to_dict()

            existing_result = await session.execute(
                select(ConversationMessage)
                .where(
                    ConversationMessage.session_id == session_uuid,
                    ConversationMessage.role == "assistant",
                    ConversationMessage.message_metadata[
                        "agent_run_id"
                    ].as_string()
                    == str(run.id),
                )
                .order_by(
                    desc(ConversationMessage.created_at),
                    desc(ConversationMessage.id),
                )
                .limit(1)
            )
            existing_message = existing_result.scalars().first()
            if existing_message is not None:
                if run.status == "cancelled":
                    existing_metadata = dict(
                        existing_message.message_metadata or {}
                    )
                    existing_metadata.update(
                        {
                            "agent_run_id": str(run.id),
                            "generation_status": "cancelled",
                            "partial": True,
                            "finish_reason": "user_stop",
                        }
                    )
                    existing_message.message_metadata = _jsonable(
                        existing_metadata
                    )
                current_result["assistant_message_id"] = str(existing_message.id)
                run.result = _jsonable(current_result)
                await session.commit()
                await session.refresh(existing_message)
                return existing_message.to_dict()

            events_result = await session.execute(
                select(AgentRunEvent)
                .where(AgentRunEvent.run_id == run.id)
                .order_by(AgentRunEvent.sequence)
            )
            snapshot = fold_cancelled_chat_snapshot(
                list(events_result.scalars().all())
            )

            from ..memory.conversation_repository import ConversationRepository

            repository = ConversationRepository(session)
            await repository._ensure_linear_parent_links(
                session,
                str(session_uuid),
            )
            parent_message_id = run.trigger_message_id
            message_is_active = True
            if parent_message_id is not None:
                trigger_message = await session.get(
                    ConversationMessage,
                    parent_message_id,
                )
                if (
                    trigger_message is None
                    or trigger_message.session_id != session_uuid
                ):
                    parent_message_id = None
                else:
                    message_is_active = bool(
                        trigger_message.is_active_branch is not False
                    )
            if parent_message_id is None:
                parent = await repository._latest_active_message(
                    session,
                    str(session_uuid),
                )
                parent_message_id = parent.id if parent else None
            branch_index = await repository._count_branch_siblings(
                session,
                str(session_uuid),
                str(parent_message_id) if parent_message_id else None,
            )

            now = datetime.utcnow()
            safe_cancel_message = sanitize_durable_error_text(message)
            safe_snapshot_content = sanitize_assistant_display_text(
                snapshot["content"]
            )
            assistant_message_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"aoitalk:cancelled-agent-run:{run.id}",
            )
            metadata: dict[str, Any] = {
                "agent_run_id": str(run.id),
                "generation_status": "cancelled",
                "partial": True,
                "finish_reason": "user_stop",
            }
            if snapshot["tool_results"]:
                metadata["tool_results"] = snapshot["tool_results"]
            if snapshot["stream_start_sequence"] is not None:
                metadata["stream_start_sequence"] = snapshot[
                    "stream_start_sequence"
                ]
            if snapshot["last_stream_token_sequence"] is not None:
                metadata["last_stream_token_sequence"] = snapshot[
                    "last_stream_token_sequence"
                ]

            assistant_message = ConversationMessage(
                id=assistant_message_id,
                session_id=session_uuid,
                role="assistant",
                content=safe_snapshot_content,
                parent_message_id=parent_message_id,
                branch_index=branch_index,
                is_active_branch=message_is_active,
                message_metadata=_jsonable(metadata),
                sender_type="assistant",
                created_at=now,
                updated_at=now,
            )
            session.add(assistant_message)

            if run.status not in {"succeeded", "failed", "cancelled"}:
                run.status = "cancelled"
                run.error = safe_cancel_message
                run.ended_at = now
                await self._append_event(
                    session,
                    run,
                    "run.cancelled",
                    status="cancelled",
                    message=safe_cancel_message,
                    payload={},
                )
            elif run.status == "cancelled" and run.ended_at is None:
                run.ended_at = now

            current_result.update(
                {
                    "assistant_message_id": str(assistant_message_id),
                    "assistant_response": safe_snapshot_content,
                    "partial": True,
                    "finish_reason": "user_stop",
                }
            )
            run.result = _jsonable(current_result)
            run.updated_at = now
            await session.execute(
                update(ConversationSession)
                .where(ConversationSession.id == session_uuid)
                .values(
                    message_count=ConversationSession.message_count + 1,
                    last_activity=_monotonic_activity(now),
                    development_status="waiting_for_user",
                )
            )
            await session.commit()
            await session.refresh(assistant_message)
            return assistant_message.to_dict()
        except IntegrityError:
            await session.rollback()
            existing_session = await self._session()
            try:
                existing_message = await existing_session.get(
                    ConversationMessage,
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"aoitalk:cancelled-agent-run:{run_uuid}",
                    ),
                )
                if existing_message is None:
                    raise RuntimeError(
                        "Cancelled chat turn persistence conflicted "
                        "without a recoverable message"
                    )
                return existing_message.to_dict()
            finally:
                await existing_session.close()
        except Exception:
            await session.rollback()
            logger.exception(
                "Failed to finalize cancelled chat turn: %s",
                run_id,
            )
            raise
        finally:
            await session.close()

    @staticmethod
    def _validate_approved_mutation_receipt(
        receipt: AgentRunToolCall,
        *,
        tool_name: str,
        tool_call_id: str,
        arguments: Any,
        metadata: Any,
    ) -> None:
        """Validate a durable winner before a retry is allowed to replay it."""

        if str(receipt.tool_call_id or "") != tool_call_id:
            raise ValueError("approved mutation receipt call id mismatch")
        if str(receipt.tool_name or "") != tool_name:
            raise ValueError("approved mutation receipt tool mismatch")
        durable_metadata = receipt.result_metadata or {}
        if not isinstance(durable_metadata, dict):
            raise ValueError("approved mutation receipt metadata is invalid")
        stored_arguments_digest = str(
            durable_metadata.get("arguments_digest") or ""
        ).strip()
        expected_arguments_digest = canonical_audit_digest(arguments)
        if stored_arguments_digest:
            if stored_arguments_digest != expected_arguments_digest:
                raise ValueError(
                    "approved mutation receipt arguments mismatch (digest)"
                )
        elif _canonical_tool_arguments(receipt.arguments) not in {
            _canonical_tool_arguments(_safe_audit_arguments(arguments)),
            _canonical_tool_arguments(arguments),
        }:
            # Rows written before the digest contract retain their safe args;
            # keep the legacy equality check for compatibility only.
            raise ValueError("approved mutation receipt arguments mismatch")
        # An approved mutation receipt is complete only when both the success
        # and mutation confirmation flags agree.  A durable failed receipt is
        # still replayable (the planning runtime will stop-first on it), but a
        # mixed/partial row is never accepted as a winner.
        if bool(receipt.success) != bool(receipt.mutation_confirmed):
            raise ValueError("approved mutation receipt confirmation mismatch")
        if durable_metadata.get("source") != "approved_plan_executor":
            raise ValueError("approved mutation receipt source mismatch")
        # Older approved receipts predate the atomic marker and digest fields.
        # They remain replayable only through the safe-args compatibility path;
        # new rows must carry both an explicit atomic marker and terminal state.
        if stored_arguments_digest or durable_metadata.get("atomic") is True:
            if durable_metadata.get("atomic") is not True:
                raise ValueError("approved mutation receipt atomic marker missing")
            if durable_metadata.get("atomic_state") not in {"committed", "failed"}:
                raise ValueError("approved mutation receipt is not committed")
        elif durable_metadata.get("atomic_state") not in {None, "committed", "failed"}:
            raise ValueError("approved mutation receipt state is invalid")
        stored_result_digest = str(durable_metadata.get("result_digest") or "").strip()
        if stored_result_digest and not _is_audit_digest(stored_result_digest):
            raise ValueError("approved mutation receipt result digest is invalid")
        expected = _approved_receipt_expected_metadata(metadata)
        expected_digest = expected.pop("arguments_digest", None)
        if not stored_arguments_digest and durable_metadata.get("atomic") is not True:
            # ``atomic`` was not present on legacy approved receipts.
            expected.pop("atomic", None)
        if expected_digest is not None and expected_digest != expected_arguments_digest:
            raise ValueError("approved mutation expected arguments digest mismatch")
        if stored_arguments_digest and expected_digest is not None:
            # The row and server-owned directive must agree on the same raw
            # canonical bytes, not merely on their redacted projections.
            if stored_arguments_digest != expected_digest:
                raise ValueError(
                    "approved mutation receipt arguments mismatch (digest)"
                )
        for key, expected_value in expected.items():
            if durable_metadata.get(key) != expected_value:
                raise ValueError(
                    f"approved mutation receipt metadata mismatch: {key}"
                )

    async def prepare_approved_mutation_receipt(
        self,
        session: AsyncSession,
        *,
        run_id: str | uuid.UUID,
        tool_name: str,
        tool_call_id: str,
        arguments: Dict[str, Any] | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> PreparedApprovedMutationReceipt:
        """Reserve one approved mutation in the caller's transaction.

        The provisional row is flushed before the resource write.  The unique
        ``(run_id, tool_call_id)`` constraint is the cross-worker fence; a
        concurrent loser rolls back only its empty transaction and returns the
        committed winner for replay.  No commit or event is performed here.
        """

        run_uuid = parse_uuid(run_id)
        normalized_tool = str(tool_name or "").strip()
        normalized_call_id = str(tool_call_id or "").strip()
        if run_uuid is None:
            raise ValueError("approved mutation requires a valid run id")
        if not normalized_tool or not normalized_call_id:
            raise ValueError("approved mutation requires tool and call id")
        run = await session.get(AgentRun, run_uuid)
        if run is None:
            raise ValueError("approved mutation run not found")

        raw_arguments = arguments or {}
        normalized_arguments = _safe_audit_arguments(raw_arguments)
        raw_arguments_digest = canonical_audit_digest(raw_arguments)
        expected_metadata = _approved_receipt_expected_metadata(metadata)
        supplied_arguments_digest = str(
            expected_metadata.get("arguments_digest") or ""
        ).strip()
        if supplied_arguments_digest and supplied_arguments_digest != raw_arguments_digest:
            raise ValueError("approved mutation expected arguments digest mismatch")
        expected_metadata["arguments_digest"] = raw_arguments_digest
        existing = await session.scalar(
            select(AgentRunToolCall).where(
                AgentRunToolCall.run_id == run_uuid,
                AgentRunToolCall.tool_call_id == normalized_call_id,
            )
        )
        if existing is not None:
            self._validate_approved_mutation_receipt(
                existing,
                tool_name=normalized_tool,
                tool_call_id=normalized_call_id,
                arguments=raw_arguments,
                metadata=expected_metadata,
            )
            return PreparedApprovedMutationReceipt(
                run_id=run_uuid,
                tool_name=normalized_tool,
                tool_call_id=normalized_call_id,
                receipt=existing,
                owned=False,
                metadata=expected_metadata,
            )

        provisional = AgentRunToolCall(
            run_id=run_uuid,
            tool_name=normalized_tool,
            tool_call_id=normalized_call_id,
            arguments=normalized_arguments,
            result=None,
            success=False,
            mutation_confirmed=False,
            result_metadata=_approved_receipt_metadata(
                expected_metadata,
                atomic_state="prepared",
            ),
            created_at=datetime.utcnow(),
        )
        session.add(provisional)
        try:
            # Explicit flush is the reservation point.  The caller must not
            # perform a resource write until this succeeds.
            await session.flush()
        except IntegrityError:
            await session.rollback()
            winner = await session.scalar(
                select(AgentRunToolCall).where(
                    AgentRunToolCall.run_id == run_uuid,
                    AgentRunToolCall.tool_call_id == normalized_call_id,
                )
            )
            if winner is None:
                raise
            self._validate_approved_mutation_receipt(
                winner,
                tool_name=normalized_tool,
                tool_call_id=normalized_call_id,
                arguments=raw_arguments,
                metadata=expected_metadata,
            )
            return PreparedApprovedMutationReceipt(
                run_id=run_uuid,
                tool_name=normalized_tool,
                tool_call_id=normalized_call_id,
                receipt=winner,
                owned=False,
                metadata=expected_metadata,
            )
        return PreparedApprovedMutationReceipt(
            run_id=run_uuid,
            tool_name=normalized_tool,
            tool_call_id=normalized_call_id,
            receipt=provisional,
            owned=True,
            metadata=expected_metadata,
        )

    async def finalize_approved_mutation_receipt(
        self,
        session: AsyncSession,
        prepared: PreparedApprovedMutationReceipt,
        *,
        arguments: Dict[str, Any] | None = None,
        result: Any = None,
        success: bool = True,
        mutation_confirmed: bool | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Stage the final receipt and audit event in the same transaction."""

        raw_arguments = arguments or {}
        normalized_arguments = _safe_audit_arguments(raw_arguments)
        if not prepared.owned:
            self._validate_approved_mutation_receipt(
                prepared.receipt,
                tool_name=prepared.tool_name,
                tool_call_id=prepared.tool_call_id,
                arguments=raw_arguments,
                metadata=prepared.metadata,
            )
            return prepared.receipt.to_dict()

        receipt = prepared.receipt
        raw_arguments_digest = canonical_audit_digest(raw_arguments)
        expected_arguments_digest = str(
            prepared.metadata.get("arguments_digest") or ""
        ).strip()
        if expected_arguments_digest and raw_arguments_digest != expected_arguments_digest:
            raise ValueError("approved mutation final arguments digest mismatch")
        if _canonical_tool_arguments(receipt.arguments) != _canonical_tool_arguments(
            normalized_arguments
        ):
            raise ValueError("approved mutation final arguments mismatch")
        effective_success = bool(success)
        effective_confirmed = (
            effective_success
            if mutation_confirmed is None
            else bool(mutation_confirmed)
        )
        if effective_confirmed != effective_success:
            raise ValueError("approved mutation confirmation must match success")
        receipt.arguments = normalized_arguments
        raw_result = result
        durable_result = _durable_tool_result(prepared.tool_name, raw_result)
        receipt.result = (
            None if durable_result is None else _safe_audit_result(durable_result)
        )
        receipt.success = effective_success
        receipt.mutation_confirmed = effective_confirmed
        merged_metadata = dict(prepared.metadata)
        if isinstance(metadata, dict):
            for key in (
                "plan_id",
                "plan_revision",
                "action_index",
                "action_digest",
            ):
                if key in metadata:
                    merged_metadata[key] = _jsonable(metadata[key])
        receipt.result_metadata = _approved_receipt_metadata(
            merged_metadata,
            atomic_state="committed" if effective_success else "failed",
        )
        receipt.result_metadata["arguments_digest"] = raw_arguments_digest
        receipt.result_metadata["result_digest"] = canonical_audit_digest(raw_result)
        await session.flush()
        run = await session.get(AgentRun, prepared.run_id)
        if run is None:
            raise ValueError("approved mutation run disappeared")
        event = await self._append_event(
            session,
            run,
            "tool.end" if effective_success else "tool.failed",
            status="succeeded" if effective_success else "failed",
            message=prepared.tool_name,
            payload={
                "tool_name": prepared.tool_name,
                "tool_call_id": prepared.tool_call_id,
                "success": effective_success,
                "mutation_confirmed": effective_confirmed,
                "source": "approved_plan_executor",
                "atomic": True,
            },
        )
        await session.flush()
        receipt.event_id = event.id
        await session.flush()
        return receipt.to_dict()

    async def record_tool_call(
        self,
        run_id: str | None,
        *,
        tool_name: str,
        arguments: Dict[str, Any] | None = None,
        result: Any = None,
        success: bool = False,
        mutation_confirmed: bool = False,
        tool_call_id: str | None = None,
        event_id: str | None = None,
        metadata: Dict[str, Any] | None = None,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        duration_ms: int | None = None,
    ) -> Dict[str, Any] | None:
        run_uuid = parse_uuid(run_id)
        if run_uuid is None or not tool_name:
            return None
        normalized_tool_call_id = str(tool_call_id or "").strip() or None

        session = await self._session()
        try:
            run = await session.get(AgentRun, run_uuid)
            if not run:
                return None
            now = datetime.utcnow()
            raw_arguments = arguments or {}
            raw_result = result
            durable_result = _durable_tool_result(tool_name, raw_result)
            # Cloud Advisor is a parent-owned advisory capability even when
            # its canonical tool is invoked from an ordinary ``chat_turn``.
            # The run type is not a sufficient write-boundary signal: generic
            # chat runs may call ``consult_cloud_advisor`` and must never
            # persist the advisory body or query in ``AgentRunToolCall``.
            cloud_advisor_tool = (
                _clean_tool_name(tool_name).casefold()
                == CLOUD_ADVISOR_TOOL_NAME
            )
            if _is_cloud_advisor_run_type(run.run_type) or cloud_advisor_tool:
                durable_result = sanitize_cloud_advisor_audit_value(
                    raw_result
                )
            operations_tool = _is_operations_tool_name(tool_name)
            safe_arguments = _safe_audit_arguments(raw_arguments)
            if operations_tool:
                # Apply the field-aware Operations projection after generic
                # secret redaction so its explicit body marker remains
                # visible while ordinary values such as ``tokenizer`` stay
                # intact.
                safe_arguments = _redact_operations_value(
                    safe_arguments,
                    tool_name=_clean_tool_name(tool_name),
                )
            if _is_cloud_advisor_run_type(run.run_type):
                safe_arguments = sanitize_cloud_advisor_audit_value(
                    raw_arguments
                )
            elif cloud_advisor_tool:
                safe_arguments = sanitize_cloud_advisor_audit_value(
                    raw_arguments
                )
            safe_result = (
                None if durable_result is None else _safe_audit_result(durable_result)
            )
            if operations_tool and isinstance(safe_result, str):
                safe_result = _redact_operations_json_arguments(
                    safe_result,
                    tool_name=_clean_tool_name(tool_name),
                )
            effective_mutation_confirmed = _mutation_confirmation_for_tool(
                tool_name,
                mutation_confirmed,
                success,
            )
            raw_metadata = metadata or {}
            safe_metadata, metadata_ok = _safe_audit_redact(raw_metadata)
            if metadata_ok and isinstance(safe_metadata, dict):
                safe_metadata = _redact_sensitive_tool_data(
                    safe_metadata,
                    tool_name=tool_name,
                )
            if not metadata_ok or not isinstance(safe_metadata, dict):
                safe_metadata = {"_redacted": AUDIT_REDACTION_FAILED_MARKER}
            if _is_cloud_advisor_run_type(run.run_type):
                safe_metadata = sanitize_cloud_advisor_audit_value(raw_metadata)
            elif cloud_advisor_tool:
                safe_metadata = sanitize_cloud_advisor_audit_value(raw_metadata)
            approved_atomic = (
                isinstance(raw_metadata, dict)
                and raw_metadata.get("source") == "approved_plan_executor"
            )
            if approved_atomic:
                safe_metadata = _approved_receipt_metadata(
                    raw_metadata,
                    atomic_state="committed" if bool(success) else "failed",
                )
                safe_metadata["arguments_digest"] = canonical_audit_digest(
                    raw_arguments
                )
                safe_metadata["result_digest"] = canonical_audit_digest(raw_result)
            elif isinstance(safe_metadata, dict):
                # Phase 2: every receipt records the canonical digest of the
                # *unredacted* intent. A later duplicate delivery can then be
                # proven identical from this digest alone, instead of comparing
                # secret-redacted payloads, where redaction can collapse two
                # genuinely distinct operations onto the same stored value.
                # Adapters that only see the observable projection of the
                # arguments propagate the digest of the restored raw intent
                # (never the raw value itself) in metadata.
                supplied_intent_digest = (
                    str(raw_metadata.get("arguments_digest") or "").strip().lower()
                    if isinstance(raw_metadata, dict)
                    else ""
                )
                safe_metadata["arguments_digest"] = (
                    supplied_intent_digest
                    if _is_audit_digest(supplied_intent_digest)
                    else canonical_audit_digest(raw_arguments)
                )
            tool_call = AgentRunToolCall(
                run_id=run.id,
                event_id=parse_uuid(event_id),
                tool_name=str(tool_name),
                tool_call_id=normalized_tool_call_id,
                arguments=safe_arguments,
                result=safe_result,
                success=bool(success),
                mutation_confirmed=effective_mutation_confirmed,
                result_metadata=safe_metadata,
                started_at=started_at,
                ended_at=ended_at,
                duration_ms=duration_ms,
                created_at=now,
            )
            session.add(tool_call)
            await self._append_event(
                session,
                run,
                "tool.end" if success else "tool.failed",
                status="succeeded" if success else "failed",
                message=str(tool_name),
                payload={
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "success": bool(success),
                    "mutation_confirmed": effective_mutation_confirmed,
                },
            )
            await session.commit()
            await session.refresh(tool_call)
            return tool_call.to_dict()
        except IntegrityError:
            await session.rollback()
            if normalized_tool_call_id:
                try:
                    existing_result = await session.execute(
                        select(AgentRunToolCall).where(
                            AgentRunToolCall.run_id == run_uuid,
                            AgentRunToolCall.tool_call_id
                            == normalized_tool_call_id,
                        )
                        .with_for_update()
                    )
                    existing = existing_result.scalar_one_or_none()
                    if existing is not None:
                        if isinstance(metadata, dict) and metadata.get(
                            "source"
                        ) == "approved_plan_executor":
                            self._validate_approved_mutation_receipt(
                                existing,
                                tool_name=str(tool_name),
                                tool_call_id=normalized_tool_call_id,
                                arguments=arguments or {},
                                metadata=metadata,
                            )
                        conflict_reason = self._tool_call_receipt_conflict_reason(
                            existing,
                            tool_name=str(tool_name),
                            arguments=arguments,
                            tool_call_id=normalized_tool_call_id,
                            # Same raw-intent digest rule as the stored receipt:
                            # the caller may pass only the projected arguments.
                            arguments_digest=(
                                safe_metadata.get("arguments_digest")
                                if isinstance(safe_metadata, dict)
                                else None
                            ),
                        )
                        if conflict_reason is not None:
                            # Fail closed: the call id was already delivered for
                            # this run with a different tool or a different
                            # canonical intent. Silently returning the earlier
                            # receipt would attribute this call's evidence and
                            # authorization to another operation.
                            logger.warning(
                                "Duplicate agent run tool call identity conflict"
                                " (run=%s, call_fingerprint=%s, reason=%s)",
                                run_uuid,
                                canonical_audit_digest(normalized_tool_call_id)[
                                    :19
                                ],
                                conflict_reason,
                            )
                            return None
                        if not await self._enrich_settled_command_observation(
                            session,
                            existing,
                            metadata=raw_metadata,
                        ):
                            return None
                        return existing.to_dict()
                except Exception:
                    logger.exception(
                        "Failed to recover duplicate agent run tool call: %s",
                        run_id,
                    )
                    return None
            logger.exception("Failed to record agent run tool call: %s", run_id)
            return None
        except Exception:
            await session.rollback()
            logger.exception("Failed to record agent run tool call: %s", run_id)
            return None
        finally:
            await session.close()

    _COMMAND_OBSERVATION_STATES = frozenset(
        {"complete", "partial", "unavailable"}
    )

    @classmethod
    def _trusted_command_observation(cls, metadata: Any) -> dict[str, Any] | None:
        """Extract the root-controlled ``command_workspace_*`` projection.

        Returns ``{}`` when the delivery carries no observation, ``None`` when
        it carries a malformed or self-inconsistent one (fail closed), else
        only the allowlisted, type-checked keys. An ``unchanged`` label is
        accepted only with complete, equal, well-formed before/after digests.
        """

        if not isinstance(metadata, dict):
            return {}
        if not any(str(k).startswith("command_workspace_") for k in metadata):
            return {}
        hex64 = re.compile(r"[0-9a-f]{64}")
        mutation = str(metadata.get("command_workspace_mutation") or "")
        before_state = metadata.get("command_workspace_before_state")
        after_state = metadata.get("command_workspace_after_state")
        before = metadata.get("command_workspace_before_digest")
        after = metadata.get("command_workspace_after_digest")
        files = metadata.get("command_workspace_files")
        if (
            mutation not in {"changed", "unchanged", "unknown"}
            or before_state not in cls._COMMAND_OBSERVATION_STATES
            or after_state not in cls._COMMAND_OBSERVATION_STATES
            or not isinstance(before, str)
            or not isinstance(after, str)
            or not all(d == "" or hex64.fullmatch(d) for d in (before, after))
            or isinstance(files, bool)
            or not isinstance(files, int)
            or files < 0
        ):
            return None
        if mutation in {"changed", "unchanged"} and not (
            before_state == "complete"
            and after_state == "complete"
            and before
            and after
            and (before == after) == (mutation == "unchanged")
        ):
            return None
        bundle = {
            "command_workspace_mutation": mutation,
            "command_workspace_before_state": before_state,
            "command_workspace_after_state": after_state,
            "command_workspace_before_digest": before,
            "command_workspace_after_digest": after,
            "command_workspace_files": files,
        }
        # Optional owned-file-scope fields: all four or none, same scope kind
        # on both sides, well-formed scope digests; ``unchanged`` additionally
        # needs equal scope digests. Absent -> legacy bundle, legacy behaviour.
        scope_keys = (
            "command_workspace_before_scope_kind",
            "command_workspace_after_scope_kind",
            "command_workspace_before_scope_sha256",
            "command_workspace_after_scope_sha256",
        )
        supplied = [k for k in scope_keys if k in metadata]
        if supplied:
            if len(supplied) != len(scope_keys):
                return None
            kind_re = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,63}")
            kb, ka, sb, sa = (metadata[k] for k in scope_keys)
            if (
                not all(isinstance(v, str) for v in (kb, ka, sb, sa))
                or not kind_re.fullmatch(kb)
                or kb != ka
                or not hex64.fullmatch(sb)
                or not hex64.fullmatch(sa)
                or (mutation == "unchanged" and sb != sa)
            ):
                return None
            bundle.update(zip(scope_keys, (kb, ka, sb, sa)))
        return bundle

    async def _enrich_settled_command_observation(
        self,
        session: Any,
        existing: Any,
        *,
        metadata: Any,
    ) -> bool:
        """Attach a late trusted workspace observation to a settled receipt.

        Identity (run, call id, tool, intent digest) was already proven by the
        caller. Only the allowlisted ``command_workspace_*`` keys are added;
        ``claim_state``/``executed``/digests/result/success are never touched.
        Returns ``False`` on any conflict so the caller rejects the delivery.
        """

        if isinstance(metadata, dict) and metadata.get("source") == "approved_plan_executor":
            return True
        observation = self._trusted_command_observation(metadata)
        if observation is None:
            return False
        if not observation:
            return True
        stored = getattr(existing, "result_metadata", None)
        if not isinstance(stored, dict):
            return False
        if (
            stored.get("claim_state") != "settled"
            or stored.get("executed") is not True
            or _clean_tool_name(getattr(existing, "tool_name", "") or "")
            .rsplit(".", 1)[-1]
            .casefold()
            not in {"execute_command", "read_command_output"}
        ):
            return False
        present = {k: stored[k] for k in observation if k in stored}
        if present:
            # First trusted observation wins; an identical resend is a no-op.
            return present == observation and len(present) == len(observation)
        existing.result_metadata = {**stored, **observation}
        await session.commit()
        return True

    async def check_tool_call_identity_collision(
        self,
        run_id: str,
        *,
        tool_call_id: str,
        tool_name: str,
        arguments: Any,
        arguments_digest: str | None = None,
    ) -> str | None:
        """Pre-dispatch fence: reason code if this call id is already bound to
        a different invocation in the same run, else ``None``.

        Callers must not dispatch (or reuse authorization/evidence) while a
        reason is returned. Lookup failure is also reported as a conflict so
        the caller fails closed instead of guessing.
        """

        normalized = str(tool_call_id or "").strip()
        if not normalized:
            return None
        try:
            run_uuid = parse_uuid(run_id)
            session = await self._session()
            try:
                result = await session.execute(
                    select(AgentRunToolCall).where(
                        AgentRunToolCall.run_id == run_uuid,
                        AgentRunToolCall.tool_call_id == normalized,
                    )
                )
                existing = result.scalar_one_or_none()
            finally:
                await session.close()
        except Exception as exc:
            # Never log the exception message/traceback: DB errors can echo
            # provider-supplied ids or bound values.
            logger.warning(
                "Tool call identity pre-dispatch check failed (error_type=%s)",
                type(exc).__name__,
            )
            return "identity_lookup_failed"
        if existing is None:
            return None
        reason = self._tool_call_receipt_conflict_reason(
            existing,
            tool_name=str(tool_name),
            arguments=arguments,
            tool_call_id=normalized,
            arguments_digest=arguments_digest,
        )
        if reason is not None:
            return reason
        return self._receipt_execution_verdict(existing)

    @staticmethod
    def _receipt_execution_verdict(existing: Any) -> str | None:
        """Verdict for a receipt whose identity is already proven."""

        metadata = getattr(existing, "result_metadata", None)
        if isinstance(metadata, str) and metadata.strip():
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = None
        executed = metadata.get("executed") if isinstance(metadata, dict) else None
        if isinstance(metadata, dict) and metadata.get("claim_state") == "pending":
            # Claimed but never settled: the effect may or may not have run.
            return "claim_pending_uncertain"
        if executed is False:
            # Explicitly never executed (e.g. validation failure): a repair is
            # not a replay.
            return None
        if executed is not True:
            # Identity is proven but execution is not (legacy/invalid row):
            # the caller must neither replay a write nor report a success.
            return "executed_state_unproven"
        if getattr(existing, "success", None) is not True:
            # Executed but failed: partial side effects are possible, so a
            # write must not be replayed automatically.
            return "executed_failed_unreconciled"
        # Identity (run, call id, tool, intent digest) is proven and the call
        # already executed successfully. The stored result rides on the verdict
        # so a write is reconciled instead of replayed; a read may refresh.
        return ExecutedReceiptMatch(str(getattr(existing, "result", "") or ""))

    async def claim_tool_call(
        self,
        run_id: str,
        *,
        tool_call_id: str,
        tool_name: str,
        arguments: Any,
        arguments_digest: str | None = None,
    ) -> str | None:
        """Atomically reserve (run_id, tool_call_id) for one intent.

        ``None`` means this caller now owns the claim and may dispatch. Any
        other value is a safe reason (or an ``ExecutedReceiptMatch`` for an
        already-successful identical call) and the caller must not dispatch.
        The unique (run_id, tool_call_id) constraint is the arbiter, so two
        workers can never both own a claim. Any DB/audit failure fails closed.
        """

        normalized = str(tool_call_id or "").strip()
        run_uuid = parse_uuid(run_id)
        if not normalized or run_uuid is None or not tool_name:
            return "claim_invalid_identity"
        supplied = str(arguments_digest or "").strip().lower()
        digest = supplied if _is_audit_digest(supplied) else canonical_audit_digest(
            arguments if isinstance(arguments, dict) else {}
        )
        try:
            session = await self._session()
        except Exception as exc:  # noqa: BLE001 - never echo the cause
            logger.warning("Tool call claim failed (error_type=%s)", type(exc).__name__)
            return "identity_lookup_failed"
        try:
            run = await session.get(AgentRun, run_uuid)
            if not run:
                return "claim_run_missing"
            session.add(
                AgentRunToolCall(
                    run_id=run_uuid,
                    tool_name=_clean_tool_name(tool_name),
                    tool_call_id=normalized,
                    arguments=_safe_audit_arguments(
                        arguments if isinstance(arguments, dict) else {}
                    ),
                    result=None,
                    success=False,
                    mutation_confirmed=False,
                    result_metadata={
                        "claim_state": "pending",
                        "arguments_digest": digest,
                    },
                    started_at=datetime.utcnow(),
                )
            )
            try:
                await session.commit()
                return None
            except IntegrityError:
                await session.rollback()
            result = await session.execute(
                select(AgentRunToolCall)
                .where(
                    AgentRunToolCall.run_id == run_uuid,
                    AgentRunToolCall.tool_call_id == normalized,
                )
                .with_for_update()
            )
            existing = result.scalar_one_or_none()
            if existing is None:
                return "claim_conflict_unreadable"
            reason = self._tool_call_receipt_conflict_reason(
                existing,
                tool_name=str(tool_name),
                arguments=arguments,
                tool_call_id=normalized,
                arguments_digest=digest,
            )
            if reason is not None:
                return reason
            verdict = self._receipt_execution_verdict(existing)
            metadata = getattr(existing, "result_metadata", None)
            if (
                verdict is None
                and isinstance(metadata, dict)
                and metadata.get("executed") is False
            ):
                # Proven never executed (validation failure): take the row over
                # for the repair under the same row lock.
                existing.result_metadata = {
                    **metadata,
                    "claim_state": "pending",
                    "executed": None,
                    "arguments_digest": digest,
                }
                await session.commit()
                return None
            return verdict or "claim_conflict_unreadable"
        except Exception as exc:  # noqa: BLE001 - never echo the cause
            logger.warning("Tool call claim failed (error_type=%s)", type(exc).__name__)
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001
                pass
            return "identity_lookup_failed"
        finally:
            await session.close()

    async def settle_tool_call(
        self,
        run_id: str,
        *,
        tool_call_id: str,
        tool_name: str,
        executed: bool,
        success: bool,
        output: Any = None,
        arguments_digest: str | None = None,
    ) -> bool:
        """Settle an owned claim with the authoritative outcome.

        ``executed=False`` releases the claim (nothing ran, so a repair may
        claim again). Otherwise the row becomes the durable receipt. Returns
        ``False`` (claim left pending, i.e. uncertain) on any failure.
        """

        normalized = str(tool_call_id or "").strip()
        run_uuid = parse_uuid(run_id)
        if not normalized or run_uuid is None:
            return False
        try:
            session = await self._session()
        except Exception:  # noqa: BLE001
            return False
        try:
            result = await session.execute(
                select(AgentRunToolCall)
                .where(
                    AgentRunToolCall.run_id == run_uuid,
                    AgentRunToolCall.tool_call_id == normalized,
                )
                .with_for_update()
            )
            row = result.scalar_one_or_none()
            metadata = getattr(row, "result_metadata", None) if row is not None else None
            if not isinstance(metadata, dict) or metadata.get("claim_state") != "pending":
                return False
            supplied = str(arguments_digest or "").strip().lower()
            if _is_audit_digest(supplied) and metadata.get("arguments_digest") != supplied:
                return False
            if not executed:
                await session.delete(row)
                await session.commit()
                return True
            durable = _durable_tool_result(tool_name, output)
            if _clean_tool_name(tool_name).casefold() == CLOUD_ADVISOR_TOOL_NAME:
                durable = None
            row.result = None if durable is None else _safe_audit_result(durable)
            row.success = bool(success)
            row.mutation_confirmed = _mutation_confirmation_for_tool(
                tool_name, False, bool(success)
            )
            row.ended_at = datetime.utcnow()
            row.result_metadata = {
                **metadata,
                "claim_state": "settled",
                "executed": True,
            }
            await session.commit()
            return True
        except Exception as exc:  # noqa: BLE001 - never echo the cause
            logger.warning(
                "Tool call settlement failed (error_type=%s)", type(exc).__name__
            )
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001
                pass
            return False
        finally:
            await session.close()

    def _tool_call_receipt_conflict_reason(
        self,
        existing: Any,
        *,
        tool_name: str,
        arguments: Any,
        tool_call_id: str = "",
        arguments_digest: str | None = None,
    ) -> str | None:
        """Prove that a duplicate delivery is the *same* invocation.

        Returns ``None`` only when the stored receipt and the incoming delivery
        share one tool name and one canonical *unredacted* intent digest. Any
        ambiguity returns a short reason code so the caller fails closed
        instead of attributing this call's evidence or authorization to an
        unrelated operation.

        Redacted payload equality is deliberately not used as identity: two
        distinct operations can redact down to the same stored value.
        """

        if tool_call_id and str(getattr(existing, "tool_call_id", "") or "") != str(
            tool_call_id
        ):
            return "tool_call_id_mismatch"
        stored_name = _clean_tool_name(getattr(existing, "tool_name", "") or "")
        incoming_name = _clean_tool_name(tool_name or "")
        if not stored_name or not incoming_name:
            return "tool_name_unknown"
        if stored_name != incoming_name:
            return "tool_name_mismatch"
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return "arguments_not_object"
        supplied = str(arguments_digest or "").strip().lower()
        incoming_digest = (
            supplied if _is_audit_digest(supplied) else canonical_audit_digest(arguments)
        )

        stored_digest = ""
        metadata = getattr(existing, "result_metadata", None)
        if isinstance(metadata, dict):
            stored_digest = str(metadata.get("arguments_digest") or "")
        elif isinstance(metadata, str) and metadata.strip():
            try:
                decoded = json.loads(metadata)
            except Exception:
                decoded = None
            if isinstance(decoded, dict):
                stored_digest = str(decoded.get("arguments_digest") or "")
        if stored_digest:
            if stored_digest == incoming_digest:
                return None
            return "arguments_digest_mismatch"

        # Legacy receipt (written before receipts carried an intent digest).
        # Identity is provable only while redaction left the stored payload
        # semantically equal to the incoming intent; otherwise fail closed.
        stored_arguments = getattr(existing, "arguments", None)
        if not isinstance(stored_arguments, dict):
            return "legacy_arguments_unprovable"
        try:
            redacted_incoming = _safe_audit_arguments(arguments)
        except Exception:
            return "legacy_arguments_unprovable"
        if canonical_audit_digest(redacted_incoming) != canonical_audit_digest(
            stored_arguments
        ):
            return "legacy_arguments_mismatch"
        if canonical_audit_digest(redacted_incoming) != incoming_digest:
            return "legacy_arguments_redacted_unprovable"
        return None

    async def create_edge(
        self,
        *,
        parent_run_id: str,
        child_run_id: str,
        purpose: str | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any] | None:
        parent_uuid = parse_uuid(parent_run_id)
        child_uuid = parse_uuid(child_run_id)
        if parent_uuid is None or child_uuid is None:
            return None

        session = await self._session()
        try:
            now = datetime.utcnow()
            edge = AgentRunEdge(
                parent_run_id=parent_uuid,
                child_run_id=child_uuid,
                purpose=purpose,
                status="open",
                edge_metadata=_safe_audit_metadata(metadata),
                created_at=now,
            )
            session.add(edge)
            await session.commit()
            await session.refresh(edge)
            return edge.to_dict()
        except Exception:
            await session.rollback()
            logger.exception("Failed to create agent run edge")
            return None
        finally:
            await session.close()

    async def close_edge(
        self,
        *,
        parent_run_id: str,
        child_run_id: str,
        status: str,
    ) -> Dict[str, Any] | None:
        """Close a parent/child edge when the child run reaches a terminal state.

        Edges are created as ``open`` so the timeline can expose an in-flight
        delegation.  The run lifecycle is persisted separately; callers must
        close the relationship explicitly once the child succeeds, fails, or
        is cancelled so API traces do not leave a completed child attached to
        a permanently-open edge.
        """

        parent_uuid = parse_uuid(parent_run_id)
        child_uuid = parse_uuid(child_run_id)
        normalized_status = str(status or "").strip().lower()
        if (
            parent_uuid is None
            or child_uuid is None
            or normalized_status not in RUN_TERMINAL_STATUSES
        ):
            return None

        session = await self._session()
        try:
            result = await session.execute(
                select(AgentRunEdge).where(
                    AgentRunEdge.parent_run_id == parent_uuid,
                    AgentRunEdge.child_run_id == child_uuid,
                )
            )
            edge = result.scalar_one_or_none()
            if edge is None:
                return None
            if edge.status in RUN_TERMINAL_STATUSES and edge.closed_at is not None:
                # Closing a Director edge is itself idempotent; preserve the
                # first terminal outcome and timestamp on duplicate callbacks.
                await session.commit()
                await session.refresh(edge)
                return edge.to_dict()
            edge.status = normalized_status
            edge.closed_at = edge.closed_at or datetime.utcnow()
            await session.commit()
            await session.refresh(edge)
            return edge.to_dict()
        except Exception:
            await session.rollback()
            logger.exception(
                "Failed to close agent run edge: %s -> %s",
                parent_run_id,
                child_run_id,
            )
            return None
        finally:
            await session.close()

    async def _settle_linked_work_in_session(
        self,
        session: Any,
        run: AgentRun,
        *,
        status: str,
        message: str | None = None,
        result: Mapping[str, Any] | None = None,
        preprovider_proof: PreproviderFailureProof | None = None,
        require_exact_interactive: bool = False,
        require_never_started: bool = False,
    ) -> bool:
        """Fenced terminal projection from AgentRun to its Work aggregate."""

        work_id = getattr(run, "work_item_id", None)
        if work_id is None:
            return False
        try:
            locked = await session.execute(
                select(AgentWorkItem)
                .where(AgentWorkItem.id == work_id)
                .with_for_update()
            )
            work = locked.scalars().first()
        except Exception:
            work = await session.get(AgentWorkItem, work_id)
        if work is None:
            return False
        active_run_id = getattr(work, "active_agent_run_id", None)
        if require_exact_interactive:
            if (
                str(getattr(work, "source_type", "") or "") != "chat_turn"
                or str(getattr(work, "execution_adapter", "") or "") != "interactive_chat"
                or active_run_id != run.id
                or getattr(run, "work_item_id", None) != work.id
                or getattr(run, "session_id", None) != getattr(work, "conversation_session_id", None)
                or getattr(run, "agent_id", None) != getattr(work, "assigned_agent_id", None)
                or getattr(run, "agent_revision_id", None) != getattr(work, "agent_revision_id", None)
                or str(getattr(work, "source_id", "") or "") != str(run.id)
                or str(getattr(work, "source_revision", "") or "") != str(getattr(run, "agent_revision_id", "") or "")
                or str(getattr(work, "intent_key", "") or "") != "interactive_chat"
            ):
                return False
        if active_run_id not in (None, run.id):
            # Another worker owns/reclaimed the lease.  A late run callback
            # must not terminalize that newer attempt.
            return False
        state = str(getattr(work, "state", "") or "")
        if state in {"succeeded", "failed", "cancelled", "dead_letter"}:
            return False
        # A Work settlement may intentionally leave ``retry_wait`` or
        # ``uncertain`` durable for recovery.  The run terminal callback must
        # not collapse that decision into ``failed`` and erase retry/fence
        # semantics; only an in-flight/pending exact item can be projected.
        if state not in {"pending", "claimed", "running"}:
            return False
        if require_never_started and (
            state != "pending"
            or int(getattr(work, "attempt_count", 0) or 0) != 0
            or getattr(work, "started_at", None) is not None
            or getattr(work, "lease_owner", None) is not None
            or getattr(work, "lease_token", None) is not None
            or getattr(work, "lease_expires_at", None) is not None
        ):
            return False
        if state in {"claimed", "running"}:
            expires = getattr(work, "lease_expires_at", None)
            if (
                getattr(work, "lease_owner", None)
                and getattr(work, "lease_token", None)
                and expires is not None
                and expires > datetime.utcnow()
            ):
                return False
        target = (
            "succeeded"
            if status == "succeeded"
            else "cancelled"
            if status == "cancelled"
            else "failed"
        )
        safe_error_code = None
        if isinstance(result, Mapping):
            candidate_error_code = result.get("safe_error_code")
            if isinstance(candidate_error_code, str) and re.fullmatch(
                r"[a-z][a-z0-9_]{0,95}", candidate_error_code
            ):
                safe_error_code = candidate_error_code
        now = datetime.utcnow()
        work.state = target
        work.outcome_classification = "succeeded" if target == "succeeded" else "cancelled" if target == "cancelled" else "permanent"
        work.lease_owner = None
        work.lease_token = None
        work.lease_expires_at = None
        work.heartbeat_at = now
        work.updated_at = now
        work.completed_at = now
        if target == "cancelled":
            work.cancelled_at = now
        if target != "succeeded":
            work.safe_error_code = (
                safe_error_code
                or ("cancelled" if target == "cancelled" else "agent_run_failed")
            )
            work.escalation_reason = sanitize_durable_error_text(message or "Agent run failed")
        if result is not None:
            work.result_summary_json = _jsonable(dict(result))
        work.active_agent_run_id = None
        # AgentWorkEvent uses a separate sequence per WorkItem; query the
        # aggregate's current tail without importing the runtime module.
        work_sequence = await session.scalar(
            select(func.max(AgentWorkEvent.sequence))
            .where(AgentWorkEvent.work_item_id == work.id)
        )
        work_event_payload = {
            "source": "agent_run_terminal",
            "status": status,
            "run_id": str(run.id),
        }
        work_proof = _trusted_preprovider_failure_proof(
            typed_proof=preprovider_proof,
            run_id=run.id,
        )
        if work_proof is not None:
            work_event_payload.update(
                {
                    "safe_error_code": _PREPROVIDER_SAFE_ERROR_CODE,
                    "provider_started": False,
                    "preprovider_proof": work_proof,
                }
            )
        session.add(
            AgentWorkEvent(
                id=uuid.uuid4(),
                work_item_id=work.id,
                sequence=int(work_sequence or 0) + 1,
                event_type=f"work.{target}",
                from_state=state,
                to_state=target,
                actor_kind="service",
                actor_id="aoitalk.system",
                actor_service_key="aoitalk.system",
                agent_run_id=run.id,
                safe_error_code=work.safe_error_code,
                result_summary=sanitize_durable_error_text(message) if message else None,
                payload_json=work_event_payload,
                created_at=now,
            )
        )
        return True

    async def reconcile_deadletter_interactive_work_in_session(
        self,
        session: Any,
        *,
        session_id: str | uuid.UUID,
        agent_id: str | uuid.UUID,
        agent_revision_id: str | uuid.UUID,
        user_id: str | None = None,
        limit: int = 100,
    ) -> int:
        """Settle only an exact, never-started Work row left by deadlettering.

        Dispatch deadlettering and live admission share the caller's
        transaction.  A failed dispatch can therefore leave its interactive
        Work row pending while the run/outbox are already terminal.  Before a
        fresh turn checks the session busy fence, reconcile that one proven
        stale row in the same transaction.  Every identity link is checked;
        a missing, live, reclaimed, or uncertain attempt is left untouched.
        """

        session_uuid = parse_uuid(session_id)
        agent_uuid = parse_uuid(agent_id)
        revision_uuid = parse_uuid(agent_revision_id)
        if session_uuid is None or agent_uuid is None or revision_uuid is None:
            return 0
        bounded_limit = max(1, min(int(limit or 1), 500))
        result = await session.execute(
            select(AgentWorkItem)
            .where(
                AgentWorkItem.source_type == "chat_turn",
                AgentWorkItem.execution_adapter == "interactive_chat",
                AgentWorkItem.conversation_session_id == session_uuid,
                AgentWorkItem.assigned_agent_id == agent_uuid,
                AgentWorkItem.agent_revision_id == revision_uuid,
                AgentWorkItem.state == "pending",
                AgentWorkItem.attempt_count == 0,
                AgentWorkItem.active_agent_run_id.is_not(None),
                AgentWorkItem.lease_owner.is_(None),
                AgentWorkItem.lease_token.is_(None),
                AgentWorkItem.lease_expires_at.is_(None),
                AgentWorkItem.started_at.is_(None),
            )
            .order_by(AgentWorkItem.updated_at, AgentWorkItem.id)
            .limit(bounded_limit)
            .with_for_update()
        )
        candidates = list(result.scalars().all())
        reconciled = 0
        expected_user_id = str(user_id) if user_id is not None else None
        for work in candidates:
            run_id = parse_uuid(getattr(work, "active_agent_run_id", None))
            if run_id is None:
                continue
            run = await session.get(AgentRun, run_id)
            if run is None:
                continue
            if (
                str(getattr(run, "status", "") or "") != "failed"
                or str(getattr(run, "run_type", "") or "") != "chat_turn"
                or parse_uuid(getattr(run, "session_id", None)) != session_uuid
                or parse_uuid(getattr(run, "agent_id", None)) != agent_uuid
                or parse_uuid(getattr(run, "agent_revision_id", None)) != revision_uuid
                or parse_uuid(getattr(run, "work_item_id", None)) != work.id
                or str(getattr(work, "source_id", "") or "") != str(run.id)
                or str(getattr(work, "source_revision", "") or "") != str(revision_uuid)
            ):
                continue
            run_user_id = str(getattr(run, "user_id", "") or "")
            if expected_user_id is not None and run_user_id != expected_user_id:
                continue
            outbox = await session.get(ConversationDispatchOutbox, run.id)
            if outbox is None or str(getattr(outbox, "status", "") or "") != DISPATCH_DEADLETTER_STATUS:
                continue
            if (
                parse_uuid(getattr(outbox, "session_id", None)) != session_uuid
                or str(getattr(outbox, "user_id", "") or "") != run_user_id
                or (expected_user_id is not None and str(getattr(outbox, "user_id", "") or "") != expected_user_id)
            ):
                continue
            if await self._settle_linked_work_in_session(
                session,
                run,
                status="failed",
                message="Dispatch retry budget exhausted",
                result={
                    "source": "dispatch_deadletter_admission_reconcile",
                    "run_id": str(run.id),
                },
                require_exact_interactive=True,
                require_never_started=True,
            ):
                reconciled += 1
        return reconciled

    async def _has_terminal_preprovider_zero_settlement_proof(
        self,
        session: Any,
        run: AgentRun,
        dialogue_id: Any,
    ) -> bool:
        """Recognize only an immutable terminal proof on a settlement retry."""

        run_id = parse_uuid(run.id)
        work_id = parse_uuid(getattr(run, "work_item_id", None))
        expected_dialogue_id = parse_uuid(dialogue_id)
        if run_id is None or work_id is None or expected_dialogue_id is None:
            return False
        run_events = list(
            (
                await session.execute(
                    select(AgentRunEvent)
                    .where(AgentRunEvent.run_id == run_id)
                    .order_by(AgentRunEvent.sequence)
                )
            ).scalars().all()
        )
        work_events = list(
            (
                await session.execute(
                    select(AgentWorkEvent)
                    .where(AgentWorkEvent.work_item_id == work_id)
                    .order_by(AgentWorkEvent.sequence)
                )
            ).scalars().all()
        )
        terminal_run_events = [
            event for event in run_events if event.event_type == "run.failed"
        ]
        terminal_work_events = [
            event for event in work_events if event.event_type == "work.failed"
        ]
        if len(terminal_run_events) != 1 or len(terminal_work_events) != 1:
            return False

        def provider_evidence(value: Any) -> bool:
            if not isinstance(value, Mapping):
                return False
            if value.get("provider_started") is True or isinstance(
                value.get("usage"), Mapping
            ):
                return True
            return any(
                provider_evidence(value.get(key))
                for key in ("result", "metadata", "agent_dialogue")
            )

        if provider_evidence(run.result) or provider_evidence(run.run_metadata):
            return False
        proof_ids: set[str] = set()
        for event, payload in (
            *(
                (item, item.payload)
                for item in terminal_run_events
            ),
            *(
                (item, item.payload_json)
                for item in terminal_work_events
            ),
        ):
            if provider_evidence(payload):
                return False
            if not isinstance(payload, Mapping):
                return False
            if str(payload.get("run_id") or "") != str(run_id):
                return False
            if payload.get("provider_started") is not False:
                return False
            if str(payload.get("safe_error_code") or "") != _PREPROVIDER_SAFE_ERROR_CODE:
                return False
            proof = payload.get("preprovider_proof")
            if not isinstance(proof, Mapping):
                return False
            if (
                str(proof.get("run_id") or "") != str(run_id)
                or proof.get("provider_started") is not False
                or proof.get("sender_attempts") != 0
                or str(proof.get("safe_error_code") or "")
                != _PREPROVIDER_SAFE_ERROR_CODE
            ):
                return False
            proof_id = str(proof.get("proof_id") or "")
            if not re.fullmatch(r"[0-9a-f]{32}", proof_id):
                return False
            proof_ids.add(proof_id)
        if len(proof_ids) != 1:
            return False
        if any(
            getattr(event, "agent_run_id", None) != run_id
            for event in terminal_work_events
        ):
            return False
        reservations = list(
            (
                await session.execute(
                    select(AgentDialogueReservation)
                    .where(AgentDialogueReservation.run_id == run_id)
                    .with_for_update()
                )
            ).scalars().all()
        )
        if len(reservations) != 1:
            return False
        reservation = reservations[0]
        if (
            str(reservation.state or "") != "released"
            or reservation.dialogue_id is None
            or reservation.dialogue_id != expected_dialogue_id
            or reservation.run_id != run_id
            or int(reservation.charged_tokens or 0) != 0
            or int(reservation.charged_cost_micros or 0) != 0
        ):
            return False
        usage = await session.scalar(
            select(AgentDialogueUsage).where(
                AgentDialogueUsage.dialogue_id == reservation.dialogue_id,
                AgentDialogueUsage.run_id == run_id,
            )
        )
        return usage is None

    async def _record_agent_dialogue_usage_in_session(
        self,
        session: Any,
        run: AgentRun,
        *,
        result: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        preprovider_proof: PreproviderFailureProof | None = None,
    ) -> None:
        """Record one terminal Dialogue usage receipt in the Run transaction.

        Provider usage is an accounting input, never a retry authority.  A
        missing payload is intentionally recorded as ``unknown`` by the
        budget service; configured token/cost ceilings then fail closed on the
        next admission instead of treating an unobserved provider call as
        free.  The ``(dialogue_id, run_id)`` receipt identity makes terminal
        callback/recovery retries idempotent.
        """

        run_metadata = run.run_metadata if isinstance(run.run_metadata, Mapping) else {}
        marker = run_metadata.get("agent_dialogue")
        dialogue_role: str | None = None
        dialogue_id = (
            parse_uuid(marker.get("dialogue_id"))
            if isinstance(marker, Mapping)
            else None
        )
        if isinstance(marker, Mapping):
            dialogue_role = str(marker.get("role") or "").strip().casefold() or None
        if dialogue_id is None:
            # The initial Human-root Run predates Dialogue creation and has no
            # marker.  The durable human_run_id anchor is its typed ownership
            # proof; account it once as a root Run when the Dialogue exists.
            try:
                initial_dialogue = await session.scalar(
                    select(AgentDialogue)
                    .where(AgentDialogue.human_run_id == run.id)
                    .limit(1)
                )
            except OperationalError as exc:
                # Legacy human/Work fixtures and rolling upgrades can have
                # AgentRun without the optional Dialogue schema.  Preserve
                # the ordinary terminal path; there is no Dialogue ledger to
                # account in this transaction.
                if "agent_dialogues" in str(exc).casefold():
                    return
                raise
            if initial_dialogue is None:
                return
            dialogue_id = parse_uuid(initial_dialogue.id)
            dialogue_role = "root"
        if dialogue_role not in {"root", "worker"}:
            return
        run_id = parse_uuid(run.id)
        if dialogue_id is None or run_id is None:
            return
        if await self._has_terminal_preprovider_zero_settlement_proof(
            session,
            run,
            dialogue_id,
        ):
            return

        usage: Mapping[str, Any] | None = None
        rate_card: Mapping[str, Any] | None = None
        usage_key: str | None = None
        for source in (metadata, result, run_metadata, run.result):
            if not isinstance(source, Mapping):
                continue
            candidate = source.get("usage")
            if usage is None and isinstance(candidate, Mapping):
                usage = candidate
            candidate_rate = source.get("rate_card")
            if rate_card is None and isinstance(candidate_rate, Mapping):
                rate_card = candidate_rate
            if rate_card is None and isinstance(source.get("agent_dialogue"), Mapping):
                dialogue_marker = source.get("agent_dialogue")
                budget_marker = dialogue_marker.get("budget")
                candidate_rate = (
                    budget_marker.get("server_rate_card")
                    if isinstance(budget_marker, Mapping)
                    else None
                )
                if isinstance(candidate_rate, Mapping):
                    rate_card = candidate_rate
            if usage_key is None and source.get("usage_key") not in (None, ""):
                usage_key = str(source.get("usage_key"))

        preprovider_proof = _trusted_preprovider_failure_proof(
            typed_proof=preprovider_proof,
            run_id=run.id,
        )
        if preprovider_proof is not None and usage is None:
            # A matching proof is the only path that settles a budgeted
            # invocation without creating an unknown usage receipt.  Any
            # existing receipt or non-reserved reservation falls through to
            # the normal sticky-unknown accounting path.
            try:
                existing_usage = await session.scalar(
                    select(AgentDialogueUsage).where(
                        AgentDialogueUsage.dialogue_id == dialogue_id,
                        AgentDialogueUsage.run_id == run.id,
                    )
                )
                if existing_usage is None:
                    dialogue_row = await session.get(AgentDialogue, dialogue_id)
                    budget_root_id = parse_uuid(
                        getattr(dialogue_row, "budget_root_dialogue_id", None)
                    ) if dialogue_row is not None else None
                    reservation_scope = [
                        AgentDialogueReservation.dialogue_id == dialogue_id
                    ]
                    if budget_root_id is not None:
                        reservation_scope.append(
                            AgentDialogueReservation.budget_root_dialogue_id
                            == budget_root_id
                        )
                    reservation = await session.scalar(
                        select(AgentDialogueReservation)
                        .where(
                            or_(*reservation_scope),
                            AgentDialogueReservation.reservation_key == f"run:{run.id}",
                            AgentDialogueReservation.dialogue_id == dialogue_id,
                            AgentDialogueReservation.run_id == run.id,
                        )
                        .with_for_update()
                    )
                    if reservation is not None and str(reservation.state or "") == "reserved":
                        from .agent_dialogue_budget import release_dialogue_budget

                        await release_dialogue_budget(
                            session,
                            dialogue_id,
                            f"run:{run.id}",
                            expected_dialogue_id=dialogue_id,
                            expected_run_id=run.id,
                        )
                        return
            except OperationalError as exc:
                detail = str(exc).casefold()
                if "agent_dialogue_usage" not in detail and "agent_dialogue_reservations" not in detail:
                    raise

        if rate_card is not None:
            # A rate card is server evidence for one exact provider/model;
            # never let a Worker card price a Root Run (or vice versa).  A
            # missing identity is treated the same as a mismatch and leaves
            # the configured cost ceiling fail-closed as cost_unknown.
            try:
                from .pricing.providers import canonical_provider

                card_provider = str(rate_card.get("provider") or "").strip()
                card_model = str(rate_card.get("model") or "").strip()
                run_provider = str(getattr(run, "provider", None) or "").strip()
                run_model = str(getattr(run, "model", None) or "").strip()
                if (
                    not card_provider
                    or not card_model
                    or not run_provider
                    or not run_model
                    or canonical_provider(card_provider)
                    != canonical_provider(run_provider)
                    or card_model.casefold() != run_model.casefold()
                ):
                    rate_card = None
            except Exception:
                rate_card = None

        # Preserve a canonical provider total.  If a legacy raw payload has
        # separate reasoning tokens but no total, fold them into output once;
        # an explicit total is authoritative and must not be double-counted.
        if isinstance(usage, Mapping) and usage.get("reasoning_tokens") not in (None, ""):
            try:
                reasoning_tokens = max(0, int(usage.get("reasoning_tokens") or 0))
                normalized_usage = dict(usage)
                total_tokens = normalized_usage.get("total_tokens")
                output_includes_reasoning = bool(
                    normalized_usage.get("output_includes_reasoning", False)
                )
                input_tokens = normalized_usage.get("input_tokens")
                if input_tokens is not None and normalized_usage.get("output_tokens") is not None:
                    minimum_total = (
                        max(0, int(input_tokens))
                        + max(0, int(normalized_usage["output_tokens"]))
                        + (reasoning_tokens if not output_includes_reasoning else 0)
                    )
                    if total_tokens is None:
                        normalized_usage["total_tokens"] = minimum_total
                        normalized_usage.setdefault("total_tokens_source", "derived")
                    elif int(total_tokens) < minimum_total:
                        # A provider-reported total is authoritative.  Keep
                        # it when it is self-consistent; only raise a
                        # malformed derived total to the conservative bound.
                        if normalized_usage.get("total_tokens_source") == "derived":
                            normalized_usage["total_tokens"] = minimum_total
                usage = normalized_usage
            except (TypeError, ValueError, OverflowError):
                # The budget helper records malformed provider accounting as
                # unknown; never turn an invalid reasoning field into a free
                # or zero-token receipt.
                usage = dict(usage)

        if (
            isinstance(usage, Mapping)
            and isinstance(rate_card, Mapping)
            and usage.get("currency") in (None, "")
            and usage.get("cost_micros") is not None
            and rate_card.get("currency") not in (None, "")
        ):
            usage = dict(usage)
            usage["currency"] = str(rate_card.get("currency"))

        try:
            from .agent_dialogue_budget import (
                charge_dialogue_budget,
                record_dialogue_usage,
                release_dialogue_budget,
            )

            receipt = await record_dialogue_usage(
                session,
                dialogue_id=dialogue_id,
                run_id=run_id,
                usage=usage,
                usage_key=usage_key or f"run:{run_id}",
                rate_card=rate_card,
                role=dialogue_role,
            )
            # ``record_dialogue_usage`` deliberately returns a compact
            # idempotent/upgraded receipt and therefore may omit ``usage``.
            # Re-read the canonical receipt row so a terminal callback retry
            # never charges zero tokens/cost merely because its response was
            # compact.  The row is also the source of truth when an unknown
            # receipt is upgraded after a recovery pass.
            receipt_row = await session.scalar(
                select(AgentDialogueUsage).where(
                    AgentDialogueUsage.dialogue_id == dialogue_id,
                    AgentDialogueUsage.run_id == run_id,
                )
            )
            receipt_state = str(
                getattr(receipt_row, "state", None)
                or receipt.get("state")
                or "unknown"
            ).casefold()
            receipt_usage = receipt.get("usage")
            receipt_usage = receipt_usage if isinstance(receipt_usage, Mapping) else {}
            observed_total = getattr(receipt_row, "total_tokens", None)
            if observed_total is None:
                observed_total = receipt_usage.get("total_tokens")
            observed_cost = getattr(receipt_row, "cost_micros", None)
            if observed_cost is None:
                observed_cost = receipt_usage.get("cost_micros")
            token_known = receipt_state in {"known", "cost_unknown"} and observed_total is not None
            cost_known = receipt_state == "known" and observed_cost is not None
            observed_tokens = int(observed_total or 0) if token_known else 0
            observed_cost_micros = int(observed_cost or 0) if cost_known else 0
            dialogue_row = await session.get(AgentDialogue, dialogue_id)
            budget_owner_row = dialogue_row
            budget_root_id = parse_uuid(
                getattr(dialogue_row, "budget_root_dialogue_id", None)
            ) if dialogue_row is not None else None
            if budget_root_id is not None:
                from ..memory.models import AgentDialogueBudgetRoot

                budget_owner_row = await session.get(
                    AgentDialogueBudgetRoot,
                    budget_root_id,
                )
            token_ceiling = bool(
                budget_owner_row is not None
                and getattr(budget_owner_row, "max_tokens", None) is not None
            )
            cost_ceiling = bool(
                budget_owner_row is not None
                and getattr(budget_owner_row, "max_cost_micros", None) is not None
            )
            reservation_scope = [
                AgentDialogueReservation.dialogue_id == dialogue_id
            ]
            if budget_root_id is not None:
                reservation_scope.append(
                    AgentDialogueReservation.budget_root_dialogue_id
                    == budget_root_id
                )
            reservation = await session.scalar(
                select(AgentDialogueReservation).where(
                    or_(*reservation_scope),
                    AgentDialogueReservation.reservation_key == f"run:{run_id}",
                )
            )
            if reservation is not None and str(reservation.state or "") == "reserved":
                has_finite_capacity = bool(
                    int(reservation.reserved_tokens or 0)
                    or int(reservation.reserved_cost_micros or 0)
                )
                if not has_finite_capacity:
                    # A legacy/no-budget Dialogue still reserves its turn and
                    # attempt for idempotency.  There is no token/cost
                    # capacity to charge, so settle that row without marking
                    # the normal provider usage as an overrun.
                    await release_dialogue_budget(
                        session,
                        dialogue_id,
                        f"run:{run_id}",
                    )
                elif (token_known and token_ceiling) or (cost_known and cost_ceiling):
                    await charge_dialogue_budget(
                        session,
                        dialogue_id,
                        f"run:{run_id}",
                        # A Dialogue may cap only one accounting dimension.
                        # Do not treat usage in an uncapped dimension as an
                        # overrun of a zero reservation in the other one.
                        tokens=observed_tokens if token_ceiling else 0,
                        cost_micros=observed_cost_micros if cost_ceiling else 0,
                    )
                else:
                    # Unknown provider usage must release the in-flight
                    # reservation while leaving Dialogue.usage_state unknown;
                    # configured ceilings then reject the next admission.
                    await release_dialogue_budget(
                        session,
                        dialogue_id,
                        f"run:{run_id}",
                    )
        except Exception as exc:
            detail = str(exc).casefold()
            if "no such table" in detail and (
                "agent_dialogue_usage" in detail
                or "agent_dialogue_reservations" in detail
            ):
                # Rolling/legacy focused fixtures may have the dialogue table
                # before the additive budget tables. Preserve the terminal Run
                # state but fence future token/cost admission as unknown.
                dialogue = await session.get(AgentDialogue, dialogue_id)
                if dialogue is not None:
                    dialogue.usage_state = "unknown"
                logger.warning(
                    "AgentDialogue budget tables are unavailable; usage is unknown: %s",
                    dialogue_id,
                )
                return
            raise

    async def record_agent_dialogue_source_usage_in_session(
        self,
        session: Any,
        *,
        dialogue_id: str | uuid.UUID,
        human_run_id: str | uuid.UUID,
    ) -> bool:
        """Backfill the immutable Human source Run after Dialogue creation.

        The initial Human Run can become terminal before the worker Dialogue
        row exists.  Its ordinary terminal callback then has no Dialogue
        ledger to update.  Dialogue admission calls this narrow, same-session
        hook immediately after inserting the canonical ``human_run_id``;
        active Runs remain pending and are metered by their normal terminal
        callback later.  Identity is checked against the persisted anchor so
        a caller cannot redirect a receipt to another Run.
        """

        dialogue_uuid = parse_uuid(dialogue_id)
        human_run_uuid = parse_uuid(human_run_id)
        if dialogue_uuid is None or human_run_uuid is None:
            raise ValueError("dialogue source usage identity is invalid")
        dialogue = await session.get(AgentDialogue, dialogue_uuid)
        if dialogue is None or parse_uuid(getattr(dialogue, "human_run_id", None)) != human_run_uuid:
            raise ValueError("dialogue Human source Run anchor changed")
        run = await session.get(AgentRun, human_run_uuid)
        if run is None or str(getattr(run, "status", "") or "").casefold() not in RUN_TERMINAL_STATUSES:
            return False
        run_result = run.result if isinstance(run.result, Mapping) else None
        run_metadata = run.run_metadata if isinstance(run.run_metadata, Mapping) else None
        await self._record_agent_dialogue_usage_in_session(
            session,
            run,
            result=run_result,
            metadata=run_metadata,
        )
        return True

    async def _set_status(
        self,
        run_id: str | None,
        status: str,
        event_type: str,
        *,
        message: str | None = None,
        metadata: Dict[str, Any] | None = None,
        provider: str | None = None,
        model: str | None = None,
        result: Dict[str, Any] | None = None,
        error: str | None = None,
        started: bool = False,
        ended: bool = False,
        preprovider_proof: PreproviderFailureProof | None = None,
    ) -> Dict[str, Any] | None:
        run_uuid = parse_uuid(run_id)
        if run_uuid is None:
            return None

        status_lock = _status_lock(run_uuid)
        await status_lock.acquire()
        try:
            session = await self._session()
        except Exception:
            status_lock.release()
            raise
        try:
            if ended and status in {"failed", "cancelled"}:
                # Cancellation persistence and conversation writers lock the
                # conversation before the run. Terminal failure updates also
                # touch the conversation below, so take the same locks in the
                # same order instead of forming Run -> Session -> Run cycles.
                session_uuid = await session.scalar(
                    select(AgentRun.session_id).where(AgentRun.id == run_uuid)
                )
                if session_uuid is not None:
                    await session.execute(
                        select(ConversationSession.id)
                        .where(ConversationSession.id == session_uuid)
                        .with_for_update()
                    )
            # A duplicate mark_running after the durable start event is an
            # idempotent metadata refresh. Avoid a second SELECT/FOR UPDATE or
            # lifecycle event; the process-local status lock already fences
            # concurrent callers in this service.
            if started and status == "running":
                existing = await session.get(AgentRun, run_uuid)
                if existing is not None and existing.status == "running" and existing.started_at is not None:
                    if provider:
                        existing.provider = provider
                    if model:
                        existing.model = model
                    if metadata:
                        merged_metadata = dict(existing.run_metadata or {})
                        merged_metadata.update(
                            _safe_audit_metadata(metadata, run_type=existing.run_type)
                        )
                        existing.run_metadata = merged_metadata
                    await session.commit()
                    await session.refresh(existing)
                    return existing.to_dict()
            try:
                locked = await session.execute(
                    select(AgentRun)
                    .where(AgentRun.id == run_uuid)
                    .with_for_update()
                    # mark_running may have loaded queued before a concurrent
                    # cancellation won. Refresh that identity-map instance
                    # under the lock so terminal state cannot be resurrected.
                    .execution_options(populate_existing=True)
                )
                run = locked.scalars().first()
            except Exception:
                run = await session.get(AgentRun, run_uuid)
            if not run:
                return None
            safe_error = (
                sanitize_durable_error_text(error)
                if error is not None
                else ""
            )
            safe_status_message = (
                sanitize_durable_error_text(message)
                if event_type
                in {"run.failed", "run.cancelled", "run.awaiting_approval"}
                else message
            )
            already_started = bool(started and run.started_at is not None)
            if run.status in RUN_TERMINAL_STATUSES:
                if run.status != status:
                    # Preserve an audit trail for a conflicting terminal
                    # mutation, but never mutate terminal state twice.
                    await self._append_event(
                        session,
                        run,
                        f"{event_type}.ignored",
                        status=run.status,
                        message=safe_status_message,
                        payload={
                            "attempted_status": status,
                            "current_status": run.status,
                        },
                    )
                # Same-status retries are idempotent and must not append a
                # duplicate terminal event or rewrite ended_at/result.
                if ended and run.status in RUN_TERMINAL_STATUSES:
                    await self._record_agent_dialogue_usage_in_session(
                        session,
                        run,
                        result=result if isinstance(result, Mapping) else None,
                        metadata=metadata if isinstance(metadata, Mapping) else None,
                        preprovider_proof=preprovider_proof,
                    )
                    await self._materialize_restart_uncertain_root_notice_in_session(
                        session,
                        run,
                    )
                await session.commit()
                await session.refresh(run)
                return run.to_dict()
            now = datetime.utcnow()
            if ended and status in RUN_TERMINAL_STATUSES:
                # SQLite does not provide row-level FOR UPDATE semantics and
                # multiple processes can still race after the read. Claim
                # the first terminal transition with a compare-and-set
                # update; only its winner may append the terminal event.
                terminal_claim = await session.execute(
                    update(AgentRun)
                    .where(
                        AgentRun.id == run_uuid,
                        ~AgentRun.status.in_(tuple(RUN_TERMINAL_STATUSES)),
                    )
                    .values(status=status, updated_at=now)
                )
                if int(getattr(terminal_claim, "rowcount", 0) or 0) != 1:
                    await session.rollback()
                    winner = await session.get(AgentRun, run_uuid)
                    return winner.to_dict() if winner is not None else None
            run.status = status
            run.updated_at = now
            if started and run.started_at is None:
                run.started_at = now
            if ended:
                run.ended_at = now
                if run.app_id:
                    try:
                        from .app_git_service import AppGitService

                        git = AppGitService()
                        status_snapshot = git.status(run.app_id)
                        if not status_snapshot.get("clean"):
                            run.result_revision = git.checkpoint(
                                run.app_id,
                                f"Agent Run {run.id} 完了時 checkpoint",
                                actor=str(run.user_id) if run.user_id else None,
                            )
                        else:
                            run.result_revision = status_snapshot.get("revision")
                    except Exception:
                        logger.warning("Failed to record App result revision for run %s", run.id)
            if provider:
                run.provider = provider
            if model:
                run.model = model
            if error is not None:
                run.error = safe_error
            safe_result = (
                _sanitize_context_manifest_fields(
                    _redact_sensitive_tool_data(result)
                )
                if result is not None
                else None
            )
            if (
                safe_result is not None
                and _is_cloud_advisor_run_type(run.run_type)
            ):
                safe_result = sanitize_cloud_advisor_audit_value(safe_result)
            if isinstance(safe_result, dict) and "assistant_response" in safe_result:
                safe_result["assistant_response"] = sanitize_assistant_display_text(
                    safe_result.get("assistant_response")
                )
            if safe_result is not None:
                run.result = _jsonable(safe_result)
            if metadata:
                current = dict(run.run_metadata or {})
                current.update(metadata)
                run.run_metadata = _safe_audit_metadata(
                    current,
                    run_type=run.run_type,
                )
            if ended and status in RUN_TERMINAL_STATUSES:
                await self._record_agent_dialogue_usage_in_session(
                    session,
                    run,
                    result=safe_result if isinstance(safe_result, Mapping) else None,
                    metadata=metadata if isinstance(metadata, Mapping) else None,
                    preprovider_proof=preprovider_proof,
                )
                await self._materialize_restart_uncertain_root_notice_in_session(
                    session,
                    run,
                )
                await self._settle_linked_work_in_session(
                    session,
                    run,
                    status=status,
                    message=safe_status_message,
                    result=safe_result if isinstance(safe_result, Mapping) else None,
                    preprovider_proof=preprovider_proof,
                )
            if not already_started:
                event_payload = (
                    {"result": _jsonable(safe_result)}
                    if safe_result is not None
                    else {}
                )
                event_payload["run_id"] = str(run.id)
                event_proof = _trusted_preprovider_failure_proof(
                    typed_proof=preprovider_proof,
                    run_id=run.id,
                )
                if event_proof is not None:
                    event_payload.update(
                        {
                            "safe_error_code": _PREPROVIDER_SAFE_ERROR_CODE,
                            "provider_started": False,
                            "preprovider_proof": event_proof,
                        }
                    )
                await self._append_event(
                    session,
                    run,
                    event_type,
                    status=status,
                    message=safe_status_message,
                    payload=event_payload,
                )
            if ended and status in {"failed", "cancelled"} and run.session_id:
                # 失敗・中断時は assistant message が保存されないことがあるため、
                # ここで進行中表示を解除しないとサイドバーが回り続ける。
                await session.execute(
                    update(ConversationSession)
                    .where(
                        ConversationSession.id == run.session_id,
                        ConversationSession.development_status == "working",
                    )
                    .values(development_status="waiting_for_user")
                )
            await session.commit()
            await session.refresh(run)
            return run.to_dict()
        except Exception:
            await session.rollback()
            logger.exception("Failed to update agent run status: %s", run_id)
            return None
        finally:
            await session.close()
            _release_status_lock(run_uuid, status_lock)

    async def _append_event(
        self,
        session: AsyncSession,
        run: AgentRun,
        event_type: str,
        *,
        status: str | None = None,
        message: str | None = None,
        payload: Dict[str, Any] | None = None,
    ) -> AgentRunEvent:
        lock_result = await session.execute(
            select(AgentRun.id)
            .where(AgentRun.id == run.id)
            .with_for_update(),
            execution_options={"autoflush": False},
        )
        if lock_result.scalar_one_or_none() is None:
            raise RuntimeError("agent run disappeared while allocating event sequence")
        result = await session.execute(
            select(func.max(AgentRunEvent.sequence)).where(
                AgentRunEvent.run_id == run.id
            )
        )
        # Production sessions disable autoflush. Earlier events in this same
        # transaction are therefore not included in the database maximum.
        pending_sequence = max(
            (
                int(event.sequence or 0)
                for event in session.new
                if isinstance(event, AgentRunEvent) and event.run_id == run.id
            ),
            default=0,
        )
        sequence = max(int(result.scalar() or 0), pending_sequence) + 1
        now = datetime.utcnow()
        safe_payload = _redact_sensitive_tool_data(payload or {})
        safe_message = message
        event_tool_name = (
            _clean_tool_name(safe_payload.get("tool_name"))
            if isinstance(safe_payload, dict)
            else ""
        )
        event_type_text = str(event_type or "").strip().casefold()
        message_is_cloud_advisor = (
            str(message or "").strip().casefold()
            == CLOUD_ADVISOR_TOOL_NAME
        )
        if (
            event_tool_name.casefold() == CLOUD_ADVISOR_TOOL_NAME
            or message_is_cloud_advisor
            or "cloud_advisor" in event_type_text
        ):
            safe_payload = sanitize_cloud_advisor_audit_value(safe_payload)
            safe_message = CLOUD_ADVISOR_CONTENT_REDACTED_MARKER
        elif _is_cloud_advisor_run_type(getattr(run, "run_type", None)):
            safe_payload = sanitize_cloud_advisor_audit_value(safe_payload)
            safe_message = _sanitize_cloud_advisor_event_message(
                event_type,
                message,
            )
        event = AgentRunEvent(
            run_id=run.id,
            sequence=sequence,
            event_type=event_type,
            status=status,
            message=safe_message,
            # This is the final durable event boundary; re-apply field-aware
            # redaction even when a caller bypasses ``record_event``.
            payload=_jsonable(safe_payload),
            created_at=now,
        )
        session.add(event)
        run.last_event_at = now
        run.updated_at = now
        return event
