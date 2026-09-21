"""Service layer for durable agent run tracking."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Mapping
from contextvars import ContextVar, Token
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Any, Dict

from sqlalchemy import and_, case, delete, desc, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..memory.database import get_database_manager
from ..memory.models import (
    Agent,
    AgentRevision,
    AgentRun,
    AgentRunEdge,
    AgentRunEvent,
    AgentRunToolCall,
    AgentWorkItem,
    ConversationDispatchOutbox,
    ConversationMessage,
    ConversationSession,
    Task,
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
from ..utils.uuid_utils import parse_uuid

logger = logging.getLogger(__name__)

RUN_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
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
_AGENT_RUN_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "total_tokens",
)
_RESOURCE_MUTATION_TOOL_NAMES = frozenset(
    (*TASK_MUTATION_OPERATIONS, *DOCS_MUTATION_OPERATIONS)
)


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


def _normalized_agent_run_usage(value: Any) -> dict[str, int] | None:
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
    usage.setdefault(
        "total_tokens",
        usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
    )
    return usage


def _merge_agent_run_usage(
    current: dict[str, Any] | None,
    incoming: dict[str, Any],
) -> dict[str, int]:
    left = _normalized_agent_run_usage(current) or {}
    right = _normalized_agent_run_usage(incoming) or {}
    return {
        key: int(left.get(key, 0)) + int(right.get(key, 0))
        for key in _AGENT_RUN_USAGE_FIELDS
    }


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
    stream_start_sequence: int | None = None
    last_stream_token_sequence: int | None = None

    for event in sorted(events, key=lambda item: int(item.sequence or 0)):
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.event_type == "stream.stream_start":
            content_parts = []
            tool_results = []
            stream_start_sequence = int(event.sequence or 0)
            last_stream_token_sequence = None
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
    # Newer emitters put the correlation id on the event itself, while some
    # provider adapters only have room for it in the durable ``tool_result``
    # object.  Read both shapes (and the two historical call containers) so a
    # start/end pair does not depend on which adapter produced the event.
    sources: list[tuple[dict[str, Any], bool]] = [(payload, False)]
    for key in ("tool_result", "tool_call", "call"):
        value = payload.get(key)
        if isinstance(value, dict):
            sources.append((value, key in {"tool_call", "call"}))
    for source, include_generic_id in sources:
        keys = (
            "operation_id",
            "tool_call_id",
            "call_id",
            "agent_instance_key",
            "actor_instance_key",
        )
        if include_generic_id:
            keys = (*keys, "id")
        key = _payload_text(
            source,
            *keys,
        )
        if key:
            return key
    return ""


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

    async def create_run(
        self,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        client_message_id: str | None = None,
        request_fingerprint: str | None = None,
        project_id: str | None = None,
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
        app_id: str | None = None,
        app_target_id: str | None = None,
        base_revision: str | None = None,
        result_revision: str | None = None,
        objective: str = "",
        generation_profile: str | None = None,
        metadata: Dict[str, Any] | None = None,
        agent_id: str | None = None,
        agent_revision_id: str | None = None,
        task_id: str | None = None,
        work_item_id: str | None = None,
        acting_subagent_id: str | None = None,
        previous_attempt_run_id: str | None = None,
        execution_manifest: Dict[str, Any] | None = None,
        execution_manifest_hash: str | None = None,
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

        db_session = await self._session()
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
                await db_session.commit()
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
                run_metadata=_safe_audit_metadata(metadata, run_type="chat_turn"),
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
            db_session.add_all(
                [
                    run,
                    outbox,
                    AgentRunEvent(
                        run_id=run_id,
                        sequence=1,
                        event_type="run.queued",
                        status="queued",
                        message="Agent run queued",
                        payload={},
                        created_at=now,
                    ),
                ]
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
                        development_status="working",
                    )
                )
            await db_session.commit()
            return run.to_dict(), str(message_uuid), True
        except IntegrityError:
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
            await db_session.rollback()
            raise
        finally:
            await db_session.close()

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
                "payload": dict(payload or {}),
            }
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

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
            outbox = await session.get(ConversationDispatchOutbox, run_uuid)
            if outbox is None or outbox.status == DISPATCH_DEADLETTER_STATUS:
                return False
            if int(outbox.attempts or 0) < safe_max_attempts:
                return False
            if outbox.status not in {"pending", "claimed"}:
                return False
            outbox.status = DISPATCH_DEADLETTER_STATUS
            outbox.lease_owner = None
            outbox.lease_token = None
            outbox.lease_expires_at = None
            outbox.updated_at = now
            run = await session.get(AgentRun, run_uuid, with_for_update=True)
            if run is not None and run.status not in RUN_TERMINAL_STATUSES:
                run.status = "failed"
                run.error = safe_reason
                run.ended_at = run.ended_at or now
                run.updated_at = now
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

    async def reconcile_stale_runs_after_restart(
        self,
        *,
        limit: int = 500,
        reason: str = "Agent run interrupted by process restart",
    ) -> dict[str, Any]:
        """Fail-closed reconciliation for in-flight runs after startup.

        In-memory provider tasks/Futures are intentionally not recreated.  Any
        run that was ``running`` at startup is marked failed once and receives
        an explicit audit event.  Queued dispatch rows remain recoverable by
        the outbox worker.  Open Director edges to terminal children are also
        closed in the same transaction.
        """

        safe_limit = min(max(int(limit or 500), 1), 5000)
        session = await self._session()
        reconciled: list[str] = []
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
                run.status = "failed"
                run.error = safe_reason
                run.ended_at = run.ended_at or now
                run.updated_at = now
                metadata = dict(run.run_metadata or {})
                pending_id = metadata.get("pending_interaction_id")
                metadata.pop("pending_interaction_id", None)
                metadata.pop("pending_interaction_kind", None)
                metadata.pop("pending_interaction_revision", None)
                metadata["reconciled_after_restart"] = True
                run.run_metadata = _safe_audit_metadata(
                    metadata,
                    run_type=run.run_type,
                )
                await self._append_event(
                    session,
                    run,
                    "run.reconciled_after_restart",
                    status="failed",
                    message=safe_reason,
                    payload={
                        "previous_status": "running",
                        "pending_interaction_id": pending_id,
                        "reconciled_after_restart": True,
                    },
                )
                reconciled.append(str(run.id))

            # Director child edges are independently durable.  Close any open
            # edge whose child is already terminal, including children that
            # completed before a crash but missed the controller callback.
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
            await session.commit()
            return {
                "reconciled_run_ids": reconciled,
                "reconciled": len(reconciled),
                "closed_edges": closed_edges,
            }
        except Exception:
            await session.rollback()
            logger.warning("Failed to reconcile stale AgentRuns after restart", exc_info=True)
            return {"reconciled_run_ids": [], "reconciled": 0, "closed_edges": 0}
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
    ) -> Dict[str, Any] | None:
        return await self._set_status(
            run_id,
            "succeeded",
            "run.succeeded",
            message=message or "Agent run completed",
            result=result,
            metadata=metadata,
            ended=True,
        )

    async def fail_run(
        self,
        run_id: str | None,
        error: str,
        *,
        result: Dict[str, Any] | None = None,
        status: str = "failed",
        metadata: Dict[str, Any] | None = None,
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
            error=error,
            ended=ended,
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
            initial_run = await session.get(AgentRun, run_uuid)
            if (
                initial_run is None
                or initial_run.session_id is None
                or initial_run.run_type != "chat_turn"
            ):
                return None
            session_uuid = initial_run.session_id

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
            if not already_started:
                await self._append_event(
                    session,
                    run,
                    event_type,
                    status=status,
                    message=safe_status_message,
                    payload=(
                        {"result": _jsonable(safe_result)}
                        if safe_result is not None
                        else {}
                    ),
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
        sequence = int(result.scalar() or 0) + 1
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
