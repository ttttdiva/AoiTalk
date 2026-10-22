"""
Conversation History API Routes

Provides REST API endpoints for managing conversation sessions and messages.
"""

import logging
import re
import time
from typing import Any, Literal, Mapping, Optional
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..config_errors import (
    CharacterLookupError,
    CharacterNotFoundError,
    add_character_lookup_context,
    build_character_lookup_error,
    character_lookup_http_detail,
    character_lookup_http_status,
)
from ..llm.context_snapshot import (
    enrich_persisted_context_snapshot,
    validate_context_manifest_metadata,
)
from ..services.conversation_title_service import ensure_conversation_title
from ..services.conversation_agent_service import (
    ConversationAgentConflictError,
    ConversationAgentError,
    ConversationAgentService,
)
from ..services.agent_root_lifecycle import (
    AgentRootLifecycleError,
    project_private_root_metadata,
)
from ..services.conversation_status import project_conversation_statuses
from ..services.session_llm_runtime import release_session_agent_team_registry
from .http_cache import etag_json_response, make_weak_etag_from_payload
from .memory_selection_models import MemorySelectionResponse

logger = logging.getLogger(__name__)


def _history_completion_iso(value: Any) -> str | None:
    """Serialize status fences as explicit UTC so Web and Mobile parse alike."""

    if value is None:
        return None
    if isinstance(value, datetime):
        stamp = value
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        else:
            stamp = stamp.astimezone(timezone.utc)
        return stamp.isoformat().replace("+00:00", "Z")
    return str(value)


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


def _initial_character_greeting(char_data: dict | None) -> str | None:
    try:
        if char_data:
            greeting = char_data.get("first_message", "")
            if not greeting and char_data.get("alternate_greetings"):
                import random

                greeting = random.choice(char_data["alternate_greetings"])
            return greeting or None
    except Exception as exc:
        logger.warning("Failed to get first_message: %s", exc)
    return None


class CreateSessionMainRoute(BaseModel):
    """Optional displayed route to pin on a newly created session."""

    provider: Optional[str] = None
    model: Optional[str] = None
    effort: Optional[str] = None


class InitialUserMessageRequest(BaseModel):
    """Optional first user turn for the idempotent native session aggregate."""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1)
    client_message_id: str = Field(min_length=1, max_length=512)


class CreateSessionRequest(BaseModel):
    """Request model for creating a new session"""

    character_name: str
    project_id: Optional[str] = None
    app_id: Optional[str] = None
    app_target_id: Optional[str] = None
    # Optional canonical Operations Agent.  Character-only sessions keep the
    # legacy ``None`` value and are handled exactly as before.
    agent_id: Optional[str] = None
    initial_message: Optional[InitialUserMessageRequest] = None
    main_route: Optional[CreateSessionMainRoute] = None


class UpdateSessionRequest(BaseModel):
    """Request model for updating a session"""

    title: Optional[str] = None
    title_source: Literal["manual", "llm", "fallback"] = "manual"
    is_active: Optional[bool] = None
    project_id: Optional[str] = None
    character_name: Optional[str] = None
    app_id: Optional[str] = None
    app_target_id: Optional[str] = None
    development_status: Optional[str] = None
    is_pinned: Optional[bool] = None


class AddMessageRequest(BaseModel):
    """Request model for adding a message"""

    # Assistant/system/tool messages are server-owned outputs.  Accepting a
    # client-supplied role would let a project writer inject prompt-control
    # messages into the durable transcript.
    role: Literal["user"] = "user"
    content: str
    client_message_id: Optional[str] = Field(default=None, max_length=512)


class ImportLocalAssistantMessageRequest(BaseModel):
    """One assistant row promoted from an owned native local transcript.

    This contract is intentionally separate from :class:`AddMessageRequest`:
    normal client message writes remain user-only.  Rejecting unknown fields
    here prevents callers from smuggling prompt-control or system/tool
    metadata through the history-import path.
    """

    model_config = ConfigDict(extra="forbid")

    role: Literal["assistant"]
    content: str
    client_message_id: str = Field(min_length=1, max_length=512)


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


class ConversationAgentBindingRequest(BaseModel):
    """CAS request for selecting or clearing a session's canonical Agent."""

    agent_id: Optional[str] = Field(default=None, max_length=64)
    expected_agent_id: Optional[str] = Field(default=None, max_length=64)


class ConversationSessionAgentProjection(BaseModel):
    """Typed session DTO shared by Agent binding/history responses."""

    id: str
    user_id: str
    character_name: str
    title: str = ""
    session_start: Optional[str] = None
    last_activity: Optional[str] = None
    message_count: int = 0
    context: dict[str, Any] = Field(default_factory=dict)
    current_summary: Optional[str] = None
    is_active: bool = True
    is_pinned: bool = False
    pinned_at: Optional[str] = None
    pin_updated_at: Optional[str] = None
    deleted_at: Optional[str] = None
    project_id: Optional[str] = None
    agent_id: Optional[str] = None
    agent_binding_updated_at: Optional[str] = None
    app_id: Optional[str] = None
    app_target_id: Optional[str] = None
    development_status: Optional[str] = None
    last_read_at: Optional[str] = None
    parent_session_id: Optional[str] = None
    forked_from_message_id: Optional[str] = None
    is_group_chat: bool = False
    group_character_names: list[Any] = Field(default_factory=list)
    participants: list[dict[str, Any]] = Field(default_factory=list)
    rp_settings: dict[str, Any] = Field(default_factory=dict)
    is_unread: Optional[bool] = None
    private_agent_root: Optional[bool] = None
    purpose: Optional[str] = None
    readonly_binding: Optional[bool] = None
    private_agent_root_metadata: Optional[dict[str, Any]] = None


ConversationHistoryStatus = Literal["idle", "ready", "needs_attention", "working"]


class ConversationHistorySessionProjection(ConversationSessionAgentProjection):
    """Session row returned by the shared history list endpoint."""

    history_status: ConversationHistoryStatus = "idle"
    history_completed_at: Optional[str] = None


class ConversationHistoryListResponse(BaseModel):
    """Canonical Web/Mobile conversation history list contract."""

    success: bool = True
    conversations: list[ConversationHistorySessionProjection]
    total: int
    limit: int
    offset: int


class ConversationHistoryStatusRequest(BaseModel):
    """Batch status request used by the Web BFF bridge."""

    model_config = ConfigDict(extra="forbid")

    session_ids: list[str] = Field(min_length=1, max_length=1000)


class ConversationHistoryStatusItem(BaseModel):
    session_id: str
    history_status: ConversationHistoryStatus
    history_completed_at: Optional[str] = None


class ConversationHistoryStatusResponse(BaseModel):
    statuses: list[ConversationHistoryStatusItem]


class ConversationAgentBindingProjection(BaseModel):
    id: str
    session_id: str
    agent_id: str
    agent_revision_id: str
    revision_version: int
    agent_team_id: str
    execution_profile_id: str
    snapshot_hash: Optional[str] = None
    created_at: Optional[str] = None


class AgentConversationSessionProjection(BaseModel):
    """Minimal Agent activity DTO; private conversation state is omitted."""

    id: str
    user_id: str
    character_name: str
    title: str = ""
    session_start: Optional[str] = None
    last_activity: Optional[str] = None
    message_count: int = 0
    is_active: bool = True
    is_pinned: bool = False
    pinned_at: Optional[str] = None
    pin_updated_at: Optional[str] = None
    project_id: Optional[str] = None
    agent_id: Optional[str] = None
    agent_binding_updated_at: Optional[str] = None
    app_id: Optional[str] = None
    app_target_id: Optional[str] = None
    development_status: Optional[str] = None
    last_read_at: Optional[str] = None
    parent_session_id: Optional[str] = None
    forked_from_message_id: Optional[str] = None
    is_group_chat: bool = False
    private_agent_root: Optional[bool] = None
    purpose: Optional[str] = None
    readonly_binding: Optional[bool] = None
    private_agent_root_metadata: Optional[dict[str, Any]] = None


class ConversationAgentBindingResponse(BaseModel):
    success: bool = True
    session: ConversationSessionAgentProjection
    binding: Optional[ConversationAgentBindingProjection] = None
    control_notices: list[dict[str, Any]] = Field(default_factory=list)


class ConversationAgentSafeDTO(BaseModel):
    id: str
    display_name: str
    slug: Optional[str] = None
    state: str
    latest_revision_id: str
    latest_revision_version: int
    character_id: Optional[str] = None
    created_by: Optional[str] = None


class ConversationAgentListResponse(BaseModel):
    success: bool = True
    agents: list[ConversationAgentSafeDTO]


class AgentConversationsResponse(BaseModel):
    success: bool = True
    sessions: list[AgentConversationSessionProjection]
    total: int
    limit: int
    offset: int


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
    is_pinned: bool = False
    pinned_at: Optional[str] = None
    history_status: Optional[ConversationHistoryStatus] = None
    history_completed_at: Optional[str] = None


class ConversationSearchResponse(BaseModel):
    """Canonical conversation search response for all native clients."""

    results: list[ConversationSearchResult]
    total: int


# Content search has to decrypt rows in the API process. Keep each database
# round-trip bounded while allowing the cursor to advance across the full
# readable message history, including inactive conversation sessions.
_CONVERSATION_SEARCH_MESSAGE_PAGE_SIZE = 200
_CONVERSATION_SEARCH_SESSION_PAGE_SIZE = 200
_CORRUPT_CONVERSATION_CONTENT_ERRORS = frozenset(
    {
        "unsupported encrypted field format",
        "encrypted field authentication failed",
    }
)


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


# ContextManifest is an optional observation attached to one assistant
# message.  Keep this route's response projection deliberately explicit: a
# full Manifest is still returned under ``context_manifest`` for trusted API
# consumers, while the UI consumes ``inspector`` below and never renders raw
# JSON.  These limits are intentionally smaller than the persistence limits.
_INSPECTOR_MAX_ITEMS = 128
_INSPECTOR_MAX_TEXT = 160
_SAFE_INSPECTOR_TOKEN_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:/@+\- ]{0,159}$"
)
_CAPABILITY_DENIAL_MARKERS = frozenset(
    {
        "denied",
        "deny",
        "blocked",
        "not allowed",
        "unauthorized",
        "forbidden",
        "permission denied",
        "capability blocked",
        "tool policy blocked",
    }
)
_SNAPSHOT_PRIVATE_KEYS = frozenset(
    {
        "prompt",
        "prompt_body",
        "messages",
        "content",
        "arguments",
        "args",
        "result",
        "output",
        "model_output",
        "preview",
        "reasoning",
        "reasoning_content",
    }
)


def _safe_inspector_token(value: Any, *, limit: int = _INSPECTOR_MAX_TEXT) -> str | None:
    """Return a compact display token without accepting arbitrary prompt text."""

    if isinstance(value, bool) or value is None:
        return None
    text = str(value).replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    text = " ".join(text.split()).strip()
    if not text:
        return None
    text = text[:limit].rstrip()
    return text if _SAFE_INSPECTOR_TOKEN_RE.fullmatch(text) else None


def _safe_inspector_hash(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if re.fullmatch(r"sha256:[0-9a-f]{64}", text) else None


def _strip_snapshot_private_fields(value: Any) -> Any:
    """Drop prompt/result-shaped fields from legacy snapshot metadata.

    ``enrich_persisted_context_snapshot`` preserves unknown legacy keys for
    compatibility.  The Inspector boundary is stricter: malformed/provider
    metadata must not turn a diagnostic snapshot into a prompt or result
    exfiltration channel.  Scalar/list shape is retained for old callers.
    """

    if isinstance(value, list):
        return [_strip_snapshot_private_fields(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        str(key): _strip_snapshot_private_fields(item)
        for key, item in value.items()
        if str(key).strip().casefold() not in _SNAPSHOT_PRIVATE_KEYS
    }


def _manifest_inspector_categories(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Project snapshot components into status/count metadata only."""

    main = snapshot.get("main") if isinstance(snapshot.get("main"), dict) else snapshot
    raw = main.get("components") or main.get("categories") or []
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for item in raw[:_INSPECTOR_MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "active").strip().casefold()
        if status not in {"active", "deferred", "failed"}:
            continue
        category = _safe_inspector_token(item.get("category"))
        if not category:
            continue
        projected: dict[str, Any] = {
            "category": category,
            "status": status,
        }
        for key in (
            "measurement",
            "source",
            "selection_reason",
        ):
            value = _safe_inspector_token(item.get(key))
            if value:
                projected[key] = value
        for key in (
            "tokens",
            "percentage",
            "retrieved_chars",
            "selected_chars",
            "size_chars",
        ):
            value = item.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if value < 0:
                continue
            projected[key] = int(value) if isinstance(value, int) else round(float(value), 3)
        result.append(projected)
    return result


def _manifest_inspector_projection(
    manifest: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the non-JSON-dump structure consumed by WorkIntelligenceInspector."""

    def copy_rows(
        key: str,
        fields: tuple[str, ...],
        *,
        max_items: int = _INSPECTOR_MAX_ITEMS,
    ) -> list[dict[str, Any]]:
        raw_rows = manifest.get(key)
        if not isinstance(raw_rows, list):
            return []
        rows: list[dict[str, Any]] = []
        for raw in raw_rows[:max_items]:
            if not isinstance(raw, dict):
                continue
            row: dict[str, Any] = {}
            for field in fields:
                value = raw.get(field)
                if field.endswith("hash") or field in {
                    "ref_hash",
                    "locator_hash",
                    "decision_ref_hash",
                    "resource_ref_hash",
                    "supersedes_ref_hash",
                    "request_hash",
                    "person_ref_hash",
                    "subject_ref_hash",
                    "target_ref_hash",
                }:
                    safe = _safe_inspector_hash(value) if value is not None else None
                    if safe:
                        row[field] = safe
                    continue
                if isinstance(value, bool):
                    row[field] = value
                    continue
                if isinstance(value, (int, float)):
                    if value >= 0:
                        row[field] = int(value) if isinstance(value, int) else round(float(value), 3)
                    continue
                safe = _safe_inspector_token(value)
                if safe:
                    row[field] = safe
                elif value is None:
                    row[field] = None
            if row:
                rows.append(row)
        return rows

    subject = manifest.get("subject")
    safe_subject: dict[str, Any] = {}
    if isinstance(subject, dict):
        for key in (
            "include_project_context",
            "strict_project_scope",
            "automatic_context_suppressed",
            "verified_project_attachment",
        ):
            if type(subject.get(key)) is bool or subject.get(key) is None:
                safe_subject[key] = subject.get(key)
        scopes = subject.get("scopes")
        if isinstance(scopes, list):
            safe_subject["scopes"] = copy_rows_from_values(
                scopes,
                ("kind", "ref_hash", "relation", "source", "version", "freshness", "supersedes_ref_hash"),
                max_items=3,
            )

    inspector: dict[str, Any] = {
        "schema_version": _safe_inspector_token(manifest.get("schema_version")),
        "producer_version": _safe_inspector_token(manifest.get("producer_version")),
        "sanitizer_version": _safe_inspector_token(manifest.get("sanitizer_version")),
        "mode": _safe_inspector_token(manifest.get("mode")),
        "subject": safe_subject,
        "resources": copy_rows(
            "resources",
            ("kind", "ref_hash", "relation", "source", "version", "freshness", "supersedes_ref_hash"),
        ),
        "evidence": copy_rows(
            "evidence",
            ("kind", "locator_hash", "source_type", "version"),
        ),
        "policy_decisions": copy_rows(
            "policy_decisions",
            ("authority", "scope", "decision", "resource_ref_hash", "decision_ref_hash"),
            max_items=64,
        ),
        "layers": copy_rows(
            "layers",
            ("category", "source", "status", "inclusion_reason", "retrieved_chars", "selected_chars", "truncated", "transform"),
            max_items=32,
        ),
        "requests": copy_rows(
            "requests",
            (
                "request_index",
                "request_kind",
                "observed_provider",
                "observed_model",
                "context_window_tokens",
                "response_tokens_reserved",
                "input_tokens",
                "remaining_tokens",
                "measurement",
                "request_hash",
            ),
            max_items=32,
        ),
        "omission_reason_counts": {},
        "reproducibility_hashes": {},
        "manifest_hash": _safe_inspector_hash(manifest.get("manifest_hash")),
    }

    component_categories = manifest.get("requests")
    if isinstance(snapshot, Mapping):
        inspector["categories"] = _manifest_inspector_categories(snapshot)

    omission_counts = manifest.get("omission_reason_counts")
    if isinstance(omission_counts, dict):
        inspector["omission_reason_counts"] = {
            token: int(count)
            for key, count in omission_counts.items()
            if (token := _safe_inspector_token(key))
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count > 0
        }
    hashes = manifest.get("reproducibility_hashes")
    if isinstance(hashes, dict):
        inspector["reproducibility_hashes"] = {
            key: digest
            for key in ("turn", "context", "requests")
            if (digest := _safe_inspector_hash(hashes.get(key)))
        }
    budget = manifest.get("bundle_char_budget")
    if isinstance(budget, int) and not isinstance(budget, bool) and budget >= 0:
        inspector["bundle_char_budget"] = budget

    # ``wi-core-1`` is an additive sidecar.  Keep all references hashed and
    # omit item titles/people names, which are intentionally not part of the
    # historical Inspector contract.
    raw_work = manifest.get("work_intelligence")
    if isinstance(raw_work, dict):
        work: dict[str, Any] = {
            "schema_version": _safe_inspector_token(raw_work.get("schema_version")),
            "producer_version": _safe_inspector_token(raw_work.get("producer_version")),
            "items": [],
            "people": [],
            "evidence": [],
            "relations": [],
            "freshness": {},
            "omissions": {},
            "layers": [],
        }
        for raw in (raw_work.get("items") if isinstance(raw_work.get("items"), list) else [])[:64]:
            if not isinstance(raw, dict):
                continue
            item = {
                key: value
                for key in (
                    "kind", "ref_hash", "status", "priority", "score", "version",
                    "freshness", "uncertain", "advisory_conflict", "evidence_ref_hashes",
                )
                if (value := _safe_manifest_inspector_value(key, raw.get(key))) is not None
            }
            if item.get("ref_hash"):
                work["items"].append(item)
        for raw in (raw_work.get("people") if isinstance(raw_work.get("people"), list) else [])[:32]:
            if not isinstance(raw, dict):
                continue
            ref_hash = _safe_inspector_hash(raw.get("person_ref_hash"))
            if ref_hash:
                work["people"].append({
                    "person_ref_hash": ref_hash,
                    "evidence_count": max(0, int(raw.get("evidence_count") or 0)),
                    "score": round(float(raw.get("score") or 0), 3),
                    "uncertain": raw.get("uncertain") is True,
                })
        for key, fields, max_items in (
            ("evidence", ("kind", "locator_hash", "source_type", "version", "freshness", "uncertain"), 128),
            ("relations", ("relation_type", "subject_ref_hash", "target_kind", "target_ref_hash", "evidence_ref_hashes", "confidence", "uncertain"), 128),
            ("layers", ("category", "source", "status", "selected_chars", "item_count", "evidence_count"), 8),
        ):
            raw_rows = raw_work.get(key)
            if not isinstance(raw_rows, list):
                continue
            for raw in raw_rows[:max_items]:
                if not isinstance(raw, dict):
                    continue
                projected = {
                    field: value
                    for field in fields
                    if (value := _safe_manifest_inspector_value(field, raw.get(field))) is not None
                }
                if projected:
                    work[key].append(projected)
        for key, value in (raw_work.get("freshness") or {}).items() if isinstance(raw_work.get("freshness"), dict) else ():
            token = _safe_inspector_token(key)
            version = _safe_manifest_inspector_value("version", value)
            if token and version is not None:
                work["freshness"][token] = version
        for key, value in (raw_work.get("omissions") or {}).items() if isinstance(raw_work.get("omissions"), dict) else ():
            token = _safe_inspector_token(key)
            if token and isinstance(value, int) and not isinstance(value, bool) and value > 0:
                work["omissions"][token] = value
        inspector["work_intelligence"] = work
    return inspector


def _safe_manifest_inspector_value(key: str, value: Any) -> Any:
    """Defensive scalar projection for a validated Manifest sidecar."""

    if value is None:
        return None
    if key.endswith("hash") or key in {
        "ref_hash", "locator_hash", "decision_ref_hash", "resource_ref_hash",
        "supersedes_ref_hash", "request_hash", "person_ref_hash", "subject_ref_hash",
        "target_ref_hash",
    }:
        return _safe_inspector_hash(value)
    if key == "evidence_ref_hashes":
        if not isinstance(value, list):
            return None
        return [digest for item in value[:16] if (digest := _safe_inspector_hash(item))]
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value) if isinstance(value, int) else round(float(value), 3)
    return _safe_inspector_token(value)


def copy_rows_from_values(
    values: list[Any],
    fields: tuple[str, ...],
    *,
    max_items: int,
) -> list[dict[str, Any]]:
    """Project Manifest resource-like rows without exposing unknown fields."""

    rows: list[dict[str, Any]] = []
    for raw in values[:max_items]:
        if not isinstance(raw, dict):
            continue
        row = {
            field: value
            for field in fields
            if (value := _safe_manifest_inspector_value(field, raw.get(field))) is not None
        }
        if row:
            rows.append(row)
    return rows


def _capability_record_value(record: Any, *keys: str) -> Any:
    if isinstance(record, Mapping):
        for key in keys:
            if key in record and record[key] is not None:
                return record[key]
    for key in keys:
        try:
            value = getattr(record, key)
        except Exception:
            continue
        if value is not None:
            return value
    return None


def _capability_failure_is_denied(reason: Any) -> bool:
    normalized = str(reason or "").strip().casefold()
    return any(marker in normalized for marker in _CAPABILITY_DENIAL_MARKERS)


def _capability_reason(record: Any, success: bool) -> str:
    metadata = _capability_record_value(record, "metadata", "result_metadata", "meta")
    if not isinstance(metadata, Mapping):
        metadata = {}
    raw_reason = _capability_record_value(record, "reason", "failure", "error", "error_message")
    raw_reason = raw_reason or metadata.get("reason") or metadata.get("status")
    normalized = str(raw_reason or "").strip().casefold()
    if success:
        return "completed"
    if "cancel" in normalized:
        return "cancelled"
    if "timeout" in normalized:
        return "timed_out"
    return "failed"


async def _bound_agent_run_capabilities(
    *,
    session_id: str,
    agent_run_id: str | None,
) -> dict[str, Any]:
    """Read only tool names from the current session's durable AgentRun."""

    empty: dict[str, Any] = {"executed": [], "executed_names": []}
    normalized_run_id = str(agent_run_id or "").strip()
    if not normalized_run_id:
        return empty
    try:
        from ..services.agent_run_service import AgentRunService

        run = await AgentRunService().get_run(
            normalized_run_id,
            include_tool_calls=True,
            include_events=False,
        )
    except Exception:
        return empty
    if not isinstance(run, Mapping):
        return empty
    if str(run.get("session_id") or "").strip() != str(session_id).strip():
        # A stale/mismatched run id is not an authority for this turn.
        return empty

    entries: list[dict[str, str]] = []
    raw_calls = run.get("tool_calls")
    if not isinstance(raw_calls, list):
        raw_calls = []
    for record in raw_calls[:64]:
        name = _safe_inspector_token(
            _capability_record_value(record, "tool_name", "tool", "name", "capability")
        )
        success_value = _capability_record_value(record, "success", "successful")
        success = success_value is True or str(success_value or "").strip().casefold() in {
            "1", "true", "yes", "ok", "success", "succeeded"
        }
        raw_reason = _capability_record_value(record, "reason", "failure", "error", "error_message")
        metadata = _capability_record_value(record, "metadata", "result_metadata", "meta")
        if not raw_reason and isinstance(metadata, Mapping):
            raw_reason = metadata.get("reason") or metadata.get("status")
        denial_probes = (
            raw_reason,
            _capability_record_value(record, "result", "output"),
            metadata.get("reason") if isinstance(metadata, Mapping) else None,
            metadata.get("error") if isinstance(metadata, Mapping) else None,
        )
        if any(_capability_failure_is_denied(value) for value in denial_probes):
            # Do not disclose denied capability names, ids, or counts.
            continue
        if not name:
            continue
        entries.append({"name": name, "reason": _capability_reason(record, success)})
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        key = (entry["name"], entry["reason"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    empty["executed"] = deduped
    empty["executed_names"] = [entry["name"] for entry in deduped]
    return empty


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
        "is_pinned": bool(getattr(session, "is_pinned", False)),
        "pinned_at": (
            session.pinned_at.isoformat() if getattr(session, "pinned_at", None) else None
        ),
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
        "is_pinned": bool(getattr(session, "is_pinned", False)),
        "pinned_at": (
            session.pinned_at.isoformat() if getattr(session, "pinned_at", None) else None
        ),
    }


def create_conversation_router(
    require_auth,
    get_current_user,
    get_llm_for_title_generation=None,
    get_title_config=None,
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
        from ..memory.conversation_repository import (
            ConversationMessageIdempotencyConflict,
            ConversationRepository,
            ConversationSessionCreateConflict,
            ConversationWriteAuthorizationError,
        )

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

    def _session_to_list_dict(
        session,
        *,
        status: ConversationHistoryStatus = "idle",
        history_completed_at: Optional[str] = None,
    ) -> dict:
        """Serialize a session with the sidebar-specific unread projection."""
        payload = _session_to_api_dict(session)
        # Keep this legacy marker aligned with the cross-surface projection.
        # ``last_activity`` also moves for user/title edits, so it cannot be
        # used as an unread assistant-result fence.
        payload["is_unread"] = status == "ready"
        # ``history_status`` is the shared history filter state.  Keep the
        # additive field explicit so both clients consume the same projection.
        payload["history_status"] = status
        payload["history_completed_at"] = history_completed_at
        return payload

    async def _session_list_payload(sessions) -> list[dict]:
        """Decorate a visible page with one batch status projection."""

        session_ids = [str(getattr(session, "id", "")) for session in sessions]
        last_read_at = {
            str(getattr(session, "id", "")): getattr(session, "last_read_at", None)
            for session in sessions
        }
        statuses = await project_conversation_statuses(
            session_ids,
            last_read_at_by_session=last_read_at,
        )
        payload: list[dict] = []
        for session in sessions:
            projection = statuses.get(str(getattr(session, "id", "")))
            payload.append(
                _session_to_list_dict(
                    session,
                    status=projection.status if projection is not None else "idle",
                    history_completed_at=(
                        _history_completion_iso(projection.history_completed_at)
                        if projection is not None
                        else None
                    ),
                )
            )
        return payload

    def _session_to_api_dict(session) -> dict:
        """Serialize ordinary session data plus the durable root projection."""

        return project_private_root_metadata(
            session.to_dict(),
            getattr(session, "_private_agent_root_metadata", None),
        )

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

    def _conversation_agent_service() -> ConversationAgentService:
        config = None
        if callable(get_title_config):
            try:
                config = get_title_config()
            except Exception:
                config = None
        return ConversationAgentService(config=config)

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

        Message content is encrypted at rest, so bounded message pages are
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
        from ..security.field_crypto import FieldCryptoError

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
                # Creating another chat deactivates earlier sessions. History
                # search includes those sessions; deletion, ownership and the
                # Project ACL still determine whether they are readable.
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

            results: list[dict[str, object]] = []
            status_sessions: dict[str, Any] = {}
            lower_query = normalized.casefold()
            # ``created_at`` is normally non-null, but coalescing it keeps the
            # keyset cursor total and deterministic for legacy rows too.
            message_order_created_at = func.coalesce(
                ConversationMessage.created_at,
                datetime.min,
            )
            message_cursor: tuple[datetime, object] | None = None
            while len(results) < limit:
                message_query = (
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
                )
                if message_cursor is not None:
                    cursor_created_at, cursor_message_id = message_cursor
                    message_query = message_query.where(
                        or_(
                            message_order_created_at < cursor_created_at,
                            and_(
                                message_order_created_at == cursor_created_at,
                                ConversationMessage.id < cursor_message_id,
                            ),
                        )
                    )
                message_rows = (
                    await db_session.execute(
                        message_query.order_by(
                            desc(message_order_created_at),
                            desc(ConversationMessage.id),
                        ).limit(_CONVERSATION_SEARCH_MESSAGE_PAGE_SIZE)
                    )
                ).all()
                if not message_rows:
                    break

                for session, message in message_rows:
                    if len(results) >= limit:
                        break
                    if not await readable(session):
                        continue
                    status_sessions[str(session.id)] = session
                    try:
                        content = str(getattr(message, "content", "") or "")
                    except (FieldCryptoError, UnicodeDecodeError) as exc:
                        if (
                            isinstance(exc, FieldCryptoError)
                            and str(exc) not in _CORRUPT_CONVERSATION_CONTENT_ERRORS
                        ):
                            raise
                        # A single corrupt/tampered ciphertext must not hide
                        # otherwise searchable messages. Keep diagnostics
                        # metadata-only so content and ciphertext never enter
                        # logs.
                        logger.warning(
                            "Skipping conversation search message with unreadable content "
                            "session=%s message=%s error=%s",
                            getattr(session, "id", None),
                            getattr(message, "id", None),
                            type(exc).__name__,
                        )
                        continue
                    if lower_query not in content.casefold():
                        continue
                    results.append(
                        _conversation_search_message_result(
                            session, message, content, normalized
                        )
                    )

                last_message = message_rows[-1][1]
                last_message_created_at = getattr(last_message, "created_at", None)
                last_message_id = getattr(last_message, "id", None)
                if last_message_id is None:
                    # Every persisted message has a primary key. Stop rather
                    # than risking a repeated page for malformed legacy data.
                    break
                message_cursor = (
                    last_message_created_at or datetime.min,
                    last_message_id,
                )
                if len(message_rows) < _CONVERSATION_SEARCH_MESSAGE_PAGE_SIZE:
                    break

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
                session_order_activity = func.coalesce(
                    ConversationSession.last_activity,
                    ConversationSession.session_start,
                    datetime.min,
                )
                session_cursor: tuple[datetime, object] | None = None
                while len(results) < limit:
                    session_query = select(ConversationSession).where(
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
                    if session_cursor is not None:
                        cursor_activity, cursor_session_id = session_cursor
                        session_query = session_query.where(
                            or_(
                                session_order_activity < cursor_activity,
                                and_(
                                    session_order_activity == cursor_activity,
                                    ConversationSession.id < cursor_session_id,
                                ),
                            )
                        )
                    session_rows = (
                        await db_session.execute(
                            session_query.order_by(
                                desc(session_order_activity),
                                desc(ConversationSession.id),
                            ).limit(_CONVERSATION_SEARCH_SESSION_PAGE_SIZE)
                        )
                    ).scalars().all()
                    if not session_rows:
                        break
                    # Apply the result limit after duplicate and Project ACL
                    # checks so earlier unreadable/title+body matches cannot
                    # hide an older readable title match.
                    for session in session_rows:
                        if len(results) >= limit:
                            break
                        if (
                            str(session.id) in matched_session_ids
                            or not await readable(session)
                        ):
                            continue
                        status_sessions[str(session.id)] = session
                        results.append(
                            _conversation_search_session_result(session, normalized)
                        )
                    last_session = session_rows[-1]
                    last_session_id = getattr(last_session, "id", None)
                    if last_session_id is None:
                        break
                    session_cursor = (
                        getattr(last_session, "last_activity", None)
                        or getattr(last_session, "session_start", None)
                        or datetime.min,
                        last_session_id,
                    )
                    if len(session_rows) < _CONVERSATION_SEARCH_SESSION_PAGE_SIZE:
                        break

            # The canonical search route runs with the real AsyncSession.  A
            # few legacy adapters intentionally expose only the two historical
            # search queries and do not implement the lifecycle tables; they
            # leave the additive status fields absent rather than weakening
            # the strict canonical projection.
            if status_sessions and isinstance(db_session, AsyncSession):
                status_projections = await project_conversation_statuses(
                    list(status_sessions),
                    last_read_at_by_session={
                        session_id: getattr(session, "last_read_at", None)
                        for session_id, session in status_sessions.items()
                    },
                    db_session=db_session,
                )
                for result in results:
                    projection = status_projections.get(str(result.get("session_id") or ""))
                    if projection is None:
                        continue
                    result["history_status"] = projection.status
                    result["history_completed_at"] = (
                        _history_completion_iso(projection.history_completed_at)
                    )
            return ConversationSearchResponse(results=results, total=len(results))
        finally:
            await db_session.close()

    @router.get("", response_model=ConversationHistoryListResponse)
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
                "conversations": await _session_list_payload(sessions),
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
                    "conversations": await _session_list_payload(sessions),
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

    async def _create_session(
        payload: CreateSessionRequest,
        request: Request | None,
        client_session_id: UUID | None = None,
    ):
        """Create a new conversation session"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        started = time.monotonic()
        phase = "request"
        outcome = "failed"
        persisted_session_id = "-"
        supplied_request_id = _request_correlation_ids(request)[0] or "-"
        request_id = re.sub(r"[^A-Za-z0-9_.:-]", "_", supplied_request_id[:128])

        def mark_phase(name: str) -> None:
            nonlocal phase
            phase = name
            logger.info(
                "conversation_create phase=%s elapsed_ms=%.1f request_id=%s "
                "client_session_id=%s session_id=%s",
                phase, (time.monotonic() - started) * 1000, request_id,
                str(client_session_id or "-"), persisted_session_id,
            )

        try:
            mark_phase("principal")
            user_info = await _get_current_conversation_user(request)
            user_id = str(user_info.get("id") or "default_user")

            requested_character_name = str(payload.character_name or "").strip()
            mark_phase("character_lookup")
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
            mark_phase("requested_scope")
            await _require_project_permission(
                normalized_project_id,
                user_info,
                permission="write",
            )

            requested_agent_id = str(payload.agent_id or "").strip() or None
            if requested_agent_id:
                # Validate against the server's Agent/Revision and the
                # human/project ACL before deactivating an existing session.
                # Client supplied revision/prose data is intentionally not
                # accepted here; the binding service reads canonical rows.
                try:
                    await _conversation_agent_service().validate_agent_selection(
                        agent_id=requested_agent_id,
                        actor_user_id=user_id,
                        actor_role=user_info.get("role"),
                        project_id=normalized_project_id,
                    )
                except ConversationAgentError as exc:
                    raise HTTPException(
                        status_code=exc.status_code,
                        detail={"code": exc.code},
                    ) from exc

            normalized_app_id, normalized_app_target_id = await _validate_app_scope(
                request,
                app_id=payload.app_id,
                app_target_id=payload.app_target_id,
                project_id=normalized_project_id,
            )

            repo = ConversationRepository()

            if client_session_id is not None:
                # Resolve optional defaults before opening the aggregate write
                # transaction. An account-settings read/backfill cannot leave
                # a half-created conversation behind.
                from ..services.conversation_session_selection import (
                    merge_session_llm_settings,
                )
                from ..services.user_llm_preference_service import (
                    get_user_last_used_main_route,
                    has_explicit_last_used_route,
                    normalize_last_used_main_route,
                    read_last_used_main_route,
                )

                try:
                    user_id = str(UUID(user_id))
                except (TypeError, ValueError) as exc:
                    raise HTTPException(
                        status_code=403, detail="An authenticated account is required",
                    ) from exc
                client_route = normalize_last_used_main_route(
                    payload.main_route.model_dump() if payload.main_route else None
                )
                mark_phase("route_defaults")
                route = client_route
                if not has_explicit_last_used_route(route):
                    route = read_last_used_main_route(user_info.get("settings"))
                    if not has_explicit_last_used_route(route):
                        route = await get_user_last_used_main_route(user_id)
                context = (
                    merge_session_llm_settings({}, {"main_route": route})
                    if has_explicit_last_used_route(route) else {}
                )

                async def authorize_created_session(db_session, created):
                    current_project_id = getattr(created, "project_id", None)
                    await _require_project_permission(
                        current_project_id, user_info, permission="write",
                        db_session=db_session,
                    )
                    await _validate_app_scope(
                        request,
                        app_id=(
                            str(created.app_id)
                            if getattr(created, "app_id", None) else None
                        ),
                        app_target_id=(
                            str(created.app_target_id)
                            if getattr(created, "app_target_id", None) else None
                        ),
                        project_id=str(current_project_id) if current_project_id else None,
                        db_session=db_session,
                    )

                async def initialize_created_agent(db_session, created):
                    if not requested_agent_id:
                        return
                    await _conversation_agent_service().pin_turn_binding(
                        db_session=db_session,
                        session_id=created.id,
                        agent_id=requested_agent_id,
                        actor_user_id=user_id,
                        actor_role=user_info.get("role"),
                    )

                mark_phase("atomic_create_or_replay")
                client_create_kwargs = {
                    "client_session_id": str(client_session_id),
                    "user_id": user_id,
                    "character_name": character_name,
                    "request_payload": {
                        "character_name": character_name,
                        "project_id": (
                            str(UUID(normalized_project_id))
                            if normalized_project_id else None
                        ),
                        "app_id": normalized_app_id,
                        "app_target_id": normalized_app_target_id,
                        "agent_id": requested_agent_id,
                        "initial_message": (
                            payload.initial_message.model_dump()
                            if payload.initial_message is not None
                            else None
                        ),
                        "main_route": client_route,
                    },
                    "display_name": str(
                        user_info.get("display_name")
                        or user_info.get("username") or "default_user"
                    ),
                    "project_id": normalized_project_id,
                    "app_id": normalized_app_id,
                    "app_target_id": normalized_app_target_id,
                    "context": context,
                    "first_message": _initial_character_greeting(char_data),
                    "initial_message": (
                        payload.initial_message.model_dump()
                        if payload.initial_message is not None
                        else None
                    ),
                    "character_display_name": str(
                        char_data.get("name") or requested_character_name
                    ).strip(),
                    "authorize": authorize_created_session,
                }
                if requested_agent_id:
                    client_create_kwargs.update(
                        {
                            "agent_id": requested_agent_id,
                            "initialize_agent": initialize_created_agent,
                        }
                    )
                session, first_msg_content = await repo.create_client_session(
                    **client_create_kwargs
                )
                persisted_session_id = str(session.id)
                mark_phase("response")
                response = {"success": True, "session": _session_to_api_dict(session)}
                if first_msg_content:
                    response["first_message"] = first_msg_content
                initial_user_message = getattr(session, "_initial_user_message", None)
                if initial_user_message is not None:
                    response["initial_message"] = initial_user_message
                result = JSONResponse(response)
                outcome = "success"
                return result

            # Deactivate current active session for this user/character only
            # after all authorization and scope validation has succeeded.
            if payload.initial_message is not None:
                raise HTTPException(
                    status_code=400,
                    detail="initial_message requires the idempotent client session endpoint",
                )
            mark_phase("previous_active")
            if not requested_agent_id:
                active = await repo.get_active_session(
                    user_id,
                    character_name,
                    exclude_private_root=True,
                )
                if active:
                    # Closing the previous session is lifecycle metadata, not a
                    # new chat activity. Keep its last_activity timestamp stable
                    # so creating a new session does not reorder the old one.
                    await repo.deactivate_session(str(active.id), touch_activity=False)

            # Create new session
            mark_phase("persist_session")
            create_session_kwargs = {
                "user_id": user_id,
                "character_name": character_name,
                "title": "",  # Will be generated on first message
                "project_id": normalized_project_id,
                "app_id": normalized_app_id,
                "app_target_id": normalized_app_target_id,
                "development_status": "working" if normalized_app_id else None,
            }
            if requested_agent_id:
                async def initialize_standard_agent(db_session, created):
                    await _conversation_agent_service().pin_turn_binding(
                        db_session=db_session,
                        session_id=created.id,
                        agent_id=requested_agent_id,
                        actor_user_id=user_id,
                        actor_role=user_info.get("role"),
                    )

                create_session_kwargs.update(
                    {
                        "agent_id": requested_agent_id,
                        "owner_participant": {
                            "participant_id": user_id,
                            "display_name": str(
                                user_info.get("display_name")
                                or user_info.get("username")
                                or "default_user"
                            ),
                            "role": "owner",
                            "status": "joined",
                        },
                        "initialize_agent": initialize_standard_agent,
                        "deactivate_previous": True,
                    }
                )
            session = await repo.create_session(**create_session_kwargs)
            persisted_session_id = str(session.id)
            mark_phase("owner_participant")
            if not requested_agent_id:
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
                mark_phase("stamp_route")
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
            first_msg_content = _initial_character_greeting(char_data)

            if first_msg_content:
                mark_phase("initial_greeting")
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

            mark_phase("response")
            response = {"success": True, "session": _session_to_api_dict(session)}
            if first_msg_content:
                response["first_message"] = first_msg_content
            result = JSONResponse(response)
            outcome = "success"
            return result
        except HTTPException:
            raise
        except ConversationSessionCreateConflict as exc:
            raise HTTPException(
                status_code=409, detail="Client session creation key conflict",
            ) from exc
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

        finally:
            logger.info(
                "conversation_create_end outcome=%s phase=%s elapsed_ms=%.1f "
                "request_id=%s client_session_id=%s session_id=%s",
                outcome, phase, (time.monotonic() - started) * 1000,
                request_id, str(client_session_id or "-"), persisted_session_id,
            )

    @router.post("")
    async def create_session(
        payload: CreateSessionRequest,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Create a new conversation session"""
        return await _create_session(payload, request)

    @router.put("/client/{client_session_id}")
    async def create_client_session(
        client_session_id: UUID,
        payload: CreateSessionRequest,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Create/replay one account-scoped native local conversation UUID.

        Clients must retain the UUID for retries and must not fall back to
        POST if an older server returns 404/405 for this endpoint.
        """
        return await _create_session(payload, request, client_session_id)

    @router.patch(
        "/{session_id}/agent",
        response_model=ConversationAgentBindingResponse,
        operation_id="bind_conversation_agent",
    )
    async def bind_conversation_agent(
        session_id: str,
        payload: ConversationAgentBindingRequest,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Select or clear the one canonical Operations Agent for a session."""

        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")
        user_info = await _get_current_conversation_user(request)
        user_id = str(user_info.get("id") or "").strip()
        if not user_id:
            raise HTTPException(status_code=401, detail="Not authenticated")
        try:
            result = await _conversation_agent_service().bind_session_agent(
                session_id=session_id,
                agent_id=payload.agent_id,
                expected_agent_id=payload.expected_agent_id,
                actor_user_id=user_id,
                actor_role=user_info.get("role"),
            )
            binding_payload = None
            if result.binding is not None:
                binding_payload = ConversationAgentBindingProjection(
                    id=str(result.binding.binding_id),
                    session_id=result.binding.session_id,
                    agent_id=result.binding.agent_id,
                    agent_revision_id=result.binding.agent_revision_id,
                    revision_version=result.binding.revision_version,
                    agent_team_id=result.binding.agent_team_id,
                    execution_profile_id=result.binding.execution_profile_id,
                    snapshot_hash=result.binding.snapshot_hash,
                    created_at=result.binding.created_at,
                )
            return ConversationAgentBindingResponse(
                success=True,
                session=ConversationSessionAgentProjection.model_validate(
                    _session_to_api_dict(result.session)
                ),
                binding=binding_payload,
                control_notices=[dict(item) for item in result.control_notices],
            )
        except ConversationAgentError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code},
            ) from exc

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
                        "session": _session_to_api_dict(session),
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

    @router.post(
        "/history-status",
        response_model=ConversationHistoryStatusResponse,
        operation_id="project_conversation_history_statuses",
    )
    async def project_history_statuses(
        payload: ConversationHistoryStatusRequest,
        _: None = Depends(require_auth),
        request: Request = None,
    ) -> ConversationHistoryStatusResponse:
        """Project status for an already visible page of session IDs.

        The Web BFF uses this bounded bridge after applying its own
        visibility/project filters.  Python still applies its authenticated
        session and Project ACL before returning any status, and the service
        performs one fixed batch of lifecycle reads for the authorized IDs.
        """

        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        normalized_ids: list[str] = []
        for raw_id in payload.session_ids:
            try:
                normalized = str(UUID(str(raw_id)))
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="Invalid session_id") from exc
            if normalized not in normalized_ids:
                normalized_ids.append(normalized)

        user_info = await _get_current_conversation_user(request)
        visible_user_ids = await _get_visible_conversation_user_ids(request)
        from sqlalchemy import and_, or_, select
        from sqlalchemy.orm import selectinload

        from ..memory.database import get_database_manager
        from ..memory.models import ConversationParticipant, ConversationSession
        from ..memory.project_repository import ProjectRepository

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
        session_factory_result = get_database_manager().get_session()
        db_session = (
            await session_factory_result
            if hasattr(session_factory_result, "__await__")
            else session_factory_result
        )
        accessible_project_ids: set[UUID] | None = None
        try:
            visible_sessions = list(
                (
                    await db_session.execute(
                        select(ConversationSession)
                        .options(selectinload(ConversationSession.participants))
                        .where(
                            ConversationSession.id.in_(
                                [UUID(session_id) for session_id in normalized_ids]
                            ),
                            ConversationSession.deleted_at.is_(None),
                            or_(
                                ConversationSession.user_id.in_(visible_user_ids),
                                ConversationSession.id.in_(participant_session_ids),
                            ),
                        )
                    )
                )
                .scalars()
                .unique()
                .all()
            )
            project_sessions = [
                session
                for session in visible_sessions
                if getattr(session, "project_id", None) is not None
            ]
            if project_sessions:
                try:
                    actor_uuid = UUID(str(user_info.get("id") or ""))
                except (TypeError, ValueError) as exc:
                    raise HTTPException(
                        status_code=403,
                        detail="Project access denied",
                    ) from exc
                accessible_project_ids = set(
                    await ProjectRepository.get_accessible_project_ids(
                        db_session,
                        actor_uuid,
                    )
                )
        finally:
            await db_session.close()
        requested = set(normalized_ids)
        sessions_by_id = {
            str(session.id): session
            for session in visible_sessions
            if str(session.id) in requested
        }
        authorized_sessions = []
        for session_id in normalized_ids:
            session = sessions_by_id.get(session_id)
            if session is None:
                continue
            project_id = getattr(session, "project_id", None)
            if (
                project_id is not None
                and accessible_project_ids is not None
                and project_id not in accessible_project_ids
            ):
                raise HTTPException(
                    status_code=403,
                    detail="Project permission denied",
                )
            authorized_sessions.append(session)

        projections = await project_conversation_statuses(
            [str(session.id) for session in authorized_sessions],
            last_read_at_by_session={
                str(session.id): getattr(session, "last_read_at", None)
                for session in authorized_sessions
            },
        )
        return ConversationHistoryStatusResponse(
            statuses=[
                ConversationHistoryStatusItem(
                    session_id=session_id,
                    history_status=projections[session_id].status,
                    history_completed_at=_history_completion_iso(
                        projections[session_id].history_completed_at
                    ),
                )
                for session_id in normalized_ids
                if session_id in projections
            ]
        )

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

            return JSONResponse({"success": True, "session": _session_to_api_dict(session)})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get session: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/{session_id}/memory-selection", response_model=MemorySelectionResponse, response_model_exclude_none=True)
    async def get_session_memory_selection(
        session_id: str,
        agent_run_id: str | None = Query(default=None, max_length=128),
        message_id: str | None = Query(default=None, max_length=128),
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Read the observed turn only; never search or invoke Jev on display."""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")
        from ..services.memory_selection_observation import live_observation, project_memory_selection
        repo = ConversationRepository()
        session = await repo.get_session_by_id(session_id, with_messages=False)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        visible_user_ids = await _get_visible_conversation_user_ids(request)
        if not _session_is_visible(session, visible_user_ids):
            raise HTTPException(status_code=403, detail="Access denied")
        user_info = await _get_current_conversation_user(request)
        await _require_session_project_permission(session, user_info)
        active_messages = await repo.get_active_messages_for_turn(
            session_id, message_id=message_id, agent_run_id=agent_run_id,
        )
        current = None
        record = None
        run = None
        fork_boundary = False
        # Exact turn requests never fall back to the previous answer. A copied
        # fork row is a hard boundary even if legacy metadata survived.
        for message in reversed(active_messages):
            metadata = message.message_metadata if isinstance(message.message_metadata, dict) else {}
            if message_id and str(message.id) != message_id:
                continue
            if agent_run_id and str(metadata.get("agent_run_id") or "") != agent_run_id:
                continue
            fork_boundary = bool(metadata.get("fork_source_message_id"))
            if message.role == "assistant" and not metadata.get("fork_source_message_id"):
                current = message
                record = metadata.get("memory_selection_observation")
            # With no exact selector the latest active message owns the view,
            # including a new user turn with no response/observation yet.
            break
        if record is None and agent_run_id and not message_id and current is None and not fork_boundary:
            # A live record is valid only for a running durable run whose user
            # message still belongs to the latest active turn. Back/fork/retry
            # must not resurrect a cached old run from this process.
            from ..services.agent_run_service import AgentRunService
            run = await AgentRunService().get_run(agent_run_id, include_tool_calls=True)
            latest_active = await repo.get_active_messages_for_turn(session_id)
            latest_user = latest_active[-1] if latest_active and latest_active[-1].role == "user" else None
            user_metadata = getattr(latest_user, "message_metadata", None) or {}
            live = live_observation(str(session_id), agent_run_id)
            live_binding = live.get("binding", {}) if isinstance(live, dict) else {}
            if (isinstance(run, dict) and run.get("status") in {"queued", "running", "awaiting_approval"}
                    and str(run.get("session_id") or "") == str(session_id)
                    and str(run.get("user_id") or "") == str(user_info.get("id") or "")
                    and latest_user is not None and not user_metadata.get("fork_source_message_id")
                    and str(run.get("client_message_id") or "") == str(user_metadata.get("client_message_id") or "")
                    and bool(run.get("client_message_id"))
                    and live_binding.get("client_message_id") == run.get("client_message_id")):
                record = live
        projection = await project_memory_selection(
            record, actor_id=str(user_info.get("id") or ""), session_id=str(session_id),
            agent_run_id=agent_run_id,
        )
        record_binding = record.get("binding", {}) if isinstance(record, dict) else {}
        bound_run = str(record_binding.get("agent_run_id") or "") or None
        if current is not None:
            metadata = current.message_metadata or {}
            if str(metadata.get("agent_run_id") or "") != str(bound_run or ""):
                projection = None
        tool_recall = None
        current_metadata = getattr(current, "message_metadata", None) or {}
        tool_run_id = str(current_metadata.get("agent_run_id") or agent_run_id or "")
        if current is not None and current_metadata.get("tool_results"):
            from ..services.agent_run_service import AgentRunService
            try:
                run = await AgentRunService().get_run(tool_run_id, include_tool_calls=True)
            except Exception:
                run = None
        if (isinstance(run, dict) and str(run.get("session_id") or "") == str(session_id)
                and str(run.get("user_id") or "") == str(user_info.get("id") or "")
                and (current is not None or record is not None)):
            from ..services.memory_tool_observation import project_memory_tool_results, match_observed_original
            async def resolve_conversation(hit, identifier):
                source = await repo.get_session_by_id(identifier, with_messages=False)
                if not source or getattr(source, "deleted_at", None) is not None or not _session_is_visible(source, visible_user_ids):
                    return None
                try:
                    await _require_session_project_permission(source, user_info)
                except HTTPException:
                    return None
                # Existing semantic/text results identify the original session
                # and timestamp, not an exact message ID. Verify that original
                # still belongs to the active branch; do not invent an ID.
                stamp = hit.get("timestamp") or hit.get("created_at")
                if not stamp or hit.get("type") == "archived_summary":
                    return None
                try:
                    at = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                    if at.tzinfo is not None:
                        at = at.astimezone(timezone.utc).replace(tzinfo=None)
                except (ValueError, TypeError):
                    return None
                originals = await repo.get_active_messages_for_turn(identifier, created_at=at, role=hit.get("role"), count=100)
                original = match_observed_original(hit, originals)
                if original is None:
                    return None
                return {"kind": "conversation", "source_id": identifier, "version": None,
                        "title": str(getattr(source, "title", None) or "元の会話")[:180],
                        "excerpt": str(original.content or "")[:240], "href": f"/chat?s={identifier}"}
            tool_recall = await project_memory_tool_results(
                run.get("tool_calls") or current_metadata.get("tool_results"),
                actor_id=str(user_info.get("id") or ""), resolve_conversation=resolve_conversation,
            )
            if not tool_recall["calls"]:
                tool_recall = None
            elif not bound_run:
                bound_run = tool_run_id
        available = projection is not None or tool_recall is not None
        return {
            "success": True, "status": "available" if available else "unavailable",
            "binding": {"session_id": str(session_id), "message_id": str(current.id) if current else None,
                        "agent_run_id": bound_run if available else None,
                        "active_branch": available},
            "observation": projection,
            "tool_recall": tool_recall,
        }

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
            latest_manifest = None
            latest_inspector = None
            latest_agent_run_id: str | None = None
            latest_message_id: str | None = None
            for message in reversed(messages):
                if message.role != "assistant" or getattr(message, "is_active_branch", True) is False:
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
                    latest = _strip_snapshot_private_fields(latest)
                    # IDs are authoritative route bindings, not values read
                    # from provider metadata.  Override hostile/mismatched
                    # values rather than using setdefault.
                    latest_message_id = str(message.id)
                    latest["session_id"] = str(session_id)
                    latest["message_id"] = latest_message_id
                    raw_agent_run_id = metadata.get("agent_run_id") or candidate.get("agent_run_id")
                    latest_agent_run_id = (
                        _safe_inspector_token(raw_agent_run_id, limit=128)
                        if raw_agent_run_id is not None
                        else None
                    )

                    # A fork copies history with ``fork_source_message_id``
                    # and intentionally strips turn metadata.  Treat the
                    # marker as a hard boundary even for mixed-version rows
                    # that accidentally retained a stale Manifest.
                    raw_manifest = (
                        None
                        if metadata.get("fork_source_message_id")
                        else metadata.get("context_manifest")
                    )
                    validated_manifest = validate_context_manifest_metadata(raw_manifest)
                    # A Manifest produced while Project Context was OFF must
                    # not be resurrected by a later turn/worker.  The
                    # producer's subject flag is the only trusted indicator
                    # available in a persisted row, so fail closed here.
                    if isinstance(validated_manifest, dict):
                        subject = validated_manifest.get("subject")
                        if isinstance(subject, dict) and subject.get("include_project_context") is False:
                            validated_manifest = None
                    if validated_manifest is not None:
                        latest_manifest = validated_manifest
                        latest_inspector = _manifest_inspector_projection(
                            validated_manifest,
                            latest,
                        )
                    break

            capabilities = await _bound_agent_run_capabilities(
                session_id=str(session_id),
                agent_run_id=latest_agent_run_id,
            )
            authorized_references: list[dict[str, Any]] = []
            if latest_manifest is not None and getattr(session, "project_id", None):
                # Persisted hashes are correlation hints only. Re-resolve
                # labels/hrefs through the current Project/Docs ACL path;
                # revoked or deleted rows simply disappear.
                try:
                    from ..services.work_intelligence import (
                        resolve_work_intelligence_references,
                    )

                    actor_user_id = str(user_info.get("id") or "").strip()
                    project_id = str(getattr(session, "project_id", "") or "").strip()
                    if actor_user_id and project_id:
                        resolved = await resolve_work_intelligence_references(
                            latest_manifest,
                            actor_user_id=actor_user_id,
                            project_id=project_id,
                            session_id=str(session_id),
                        )
                        if isinstance(resolved, list):
                            authorized_references = [
                                item for item in resolved[:8] if isinstance(item, dict)
                            ]
                except Exception:
                    authorized_references = []
            # Keep the top-level response additive for existing clients while
            # exposing the same bound IDs in a compact, explicit turn marker.
            if latest is not None and latest_agent_run_id:
                latest["agent_run_id"] = latest_agent_run_id
            binding = {
                "session_id": str(session_id),
                "message_id": latest_message_id,
                "agent_run_id": latest_agent_run_id,
                "active_branch": bool(latest is not None),
            }
            return JSONResponse({
                "success": True,
                "status": "available" if latest else "unavailable",
                "snapshot": latest,
                "context_manifest": latest_manifest,
                "inspector": latest_inspector,
                "capabilities": capabilities,
                "binding": binding,
                "turn_binding": binding,
                "session_id": str(session_id),
                "message_id": latest_message_id,
                "agent_run_id": latest_agent_run_id,
                "authorized_references": authorized_references,
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
                # A client_message_id is scoped to the authenticated actor for
                # both user and assistant writes.  Without this provenance an
                # assistant retry could replay a legacy/null row on behalf of
                # another participant in the same shared conversation.
                sender_id=(
                    user_id
                    if payload.role == "user" or payload.client_message_id
                    else None
                ),
                sender_display_name=(
                    str(user_info.get("display_name") or user_info.get("username") or "")
                    if payload.role == "user"
                    else None
                ),
                actor_user_id=user_id,
                actor_role=str(user_info.get("role") or ""),
            )

            replayed = bool(getattr(message, "_idempotency_replayed", False))
            generated_title = None
            if not replayed:
                # メッセージ保存の応答は題名LLMを待たない。fallback だけ即時に返す。
                generated_title = await ensure_conversation_title(
                    repo=repo,
                    session_id=session_id,
                    llm_generator=None,
                )

            response = {
                "success": True,
                "replayed": replayed,
                "message": message.to_dict(),
            }
            if generated_title:
                response["title"] = generated_title.title
                response["title_source"] = generated_title.source
            return JSONResponse(response)
        except HTTPException:
            raise
        except ConversationMessageIdempotencyConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "idempotency_conflict",
                    "message": "同じclient_message_idの内容が一致しません",
                    "retryable": False,
                },
            ) from exc
        except ConversationWriteAuthorizationError as exc:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "forbidden",
                    "message": "会話への書き込み権限がありません",
                    "retryable": False,
                },
            ) from exc
        except Exception as e:
            logger.error(f"Failed to add message: {e}")
            # Provider/DB/crypto details stay in server logs.  The native
            # FastAPI writer shares the BFF's structured persistence contract
            # so mobile callers can distinguish a save failure without
            # receiving SQL, hostnames, or secret-bearing exception text.
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "message_persistence_failed",
                    "message": "メッセージを保存できませんでした",
                    "retryable": True,
                },
            ) from e

    @router.post("/{session_id}/local-import/messages")
    async def import_local_assistant_message(
        session_id: str,
        payload: ImportLocalAssistantMessageRequest,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Promote one assistant row from an owned native local transcript.

        Normal ``/{session_id}/messages`` remains user-only.  This dedicated
        endpoint is intentionally narrower: only the session owner may import
        history, project-backed sessions still require project write access,
        and a mandatory client id makes retries repository-idempotent.
        """
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            user_info = await _get_current_conversation_user(request)
            user_id = str(user_info.get("id") or "default_user")

            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id)
            if not session:
                raise HTTPException(status_code=404, detail="Session not found")

            # History promotion is owner-only even when the actor is a global
            # admin or a project/conversation participant.  This prevents an
            # authorized collaborator from injecting assistant transcript rows
            # into another user's local-session migration.
            if str(getattr(session, "user_id", "")) != user_id:
                raise HTTPException(
                    status_code=403,
                    detail="Conversation ownership required for local history import",
                )

            await _require_project_permission(
                getattr(session, "project_id", None),
                user_info,
                permission="write",
            )

            message = await repo.add_message(
                session_id=session_id,
                role="assistant",
                content=payload.content,
                metadata={"client_message_id": payload.client_message_id},
                sender_type=None,
                sender_id=user_id,
                sender_display_name=None,
                actor_user_id=user_id,
                actor_role=str(user_info.get("role") or ""),
            )
            replayed = bool(getattr(message, "_idempotency_replayed", False))
            return JSONResponse(
                {
                    "success": True,
                    "replayed": replayed,
                    "message": message.to_dict(),
                }
            )
        except HTTPException:
            raise
        except ConversationMessageIdempotencyConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "idempotency_conflict",
                    "message": "同じclient_message_idの内容が一致しません",
                    "retryable": False,
                },
            ) from exc
        except ConversationWriteAuthorizationError as exc:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "forbidden",
                    "message": "会話への書き込み権限がありません",
                    "retryable": False,
                },
            ) from exc
        except Exception as e:
            logger.error(f"Failed to import local assistant message: {e}")
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "message_persistence_failed",
                    "message": "メッセージを保存できませんでした",
                    "retryable": True,
                },
            ) from e

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

            async def _title_for_session(prompt: str) -> Optional[str]:
                from ..assistant.conversation_title_events import (
                    generate_title_with_session_route,
                    session_has_explicit_main_route,
                )

                if session_has_explicit_main_route(session):
                    config = get_title_config() if callable(get_title_config) else None
                    if config is None:
                        return None
                    return await generate_title_with_session_route(
                        config, session, prompt
                    )
                if get_llm_for_title_generation is None:
                    return None
                return await get_llm_for_title_generation(prompt)

            generated = await ensure_conversation_title(
                repo=repo,
                session_id=session_id,
                llm_generator=_title_for_session,
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
        # Capture the user-visible read fence before session/project ACL
        # awaits.  Completion committed during authorization must remain a
        # later unread result; the repository advances the durable fence
        # monotonically against concurrent reads.
        requested_read_at = datetime.utcnow()
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
            durable_read_at = await repo.mark_session_read_monotonic(
                session_id,
                requested_read_at,
            )
            if durable_read_at is None:
                raise HTTPException(status_code=404, detail="Session not found")
            return JSONResponse(
                {
                    "success": True,
                    "session_id": session_id,
                    "last_read_at": _history_completion_iso(durable_read_at),
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
                context = dict(session.context) if isinstance(session.context, dict) else {}
                generation = context.get("title_generation")
                if (
                    payload.title_source != "manual"
                    and isinstance(generation, dict)
                    and generation.get("source") == "manual"
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="手動で設定したタイトルは自動更新できません",
                    )
                updates["title"] = payload.title
                context["title_generation"] = {"source": payload.title_source}
                updates["context"] = context
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

            if "is_pinned" in payload.model_fields_set:
                if payload.is_pinned is None:
                    raise HTTPException(
                        status_code=400,
                        detail="is_pinnedにはtrueまたはfalseを指定してください",
                    )
                pin_changed_at = datetime.utcnow()
                updates["is_pinned"] = payload.is_pinned
                updates["pinned_at"] = pin_changed_at if payload.is_pinned else None
                updates["pin_updated_at"] = pin_changed_at

            if updates:
                # Session metadata (title, character/scope, active flag, or
                # development state) is not chat activity. Only creating a
                # session/message or receiving an agent response may move it
                # in the history list.
                await repo.update_session(
                    session_id,
                    touch_activity=False,
                    actor_user_id=user_info.get("id"),
                    **updates,
                )

            # Get updated session
            updated = await repo.get_session_by_id(session_id)

            return JSONResponse(
                {
                    "success": True,
                    "session": _session_to_api_dict(updated) if updated else None,
                }
            )
        except HTTPException:
            raise
        except AgentRootLifecycleError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
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

            deleted = await repo.delete_session(
                session_id,
                actor_user_id=user_info.get("id"),
            )

            # The process-local manual Team projection follows the durable
            # session row.  Release it only after the repository confirms the
            # delete; failures must leave the projection intact for retries.
            if deleted:
                release_session_agent_team_registry(
                    str(session.user_id) if session.user_id is not None else None,
                    session_id,
                )

            return JSONResponse({"success": deleted})
        except HTTPException:
            raise
        except AgentRootLifecycleError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
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
                    and getattr(s, "_private_agent_root_metadata", None) is None
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
                    "session": _session_to_api_dict(updated) if updated else None,
                    "messages": [m.to_dict() for m in messages],
                }
            )
        except HTTPException:
            raise
        except AgentRootLifecycleError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
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

            return JSONResponse({"success": True, "session": _session_to_api_dict(forked)})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except HTTPException:
            raise
        except AgentRootLifecycleError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
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


def create_conversation_agent_router(
    require_auth,
    get_current_user,
    *,
    config: Any | None = None,
) -> APIRouter:
    """Create the sibling Agent selector catalog route.

    It is kept separate from ``/api/conversations`` so the canonical public
    path remains ``GET /api/conversation-agents``.  The composition root must
    include this router alongside ``create_conversation_router``.
    """

    # This router owns two sibling API prefixes: the selectable Agent catalog
    # and the ACL-filtered session history for one Agent.
    router = APIRouter(tags=["conversation-agents"])

    async def actor(request: Request) -> dict[str, Any]:
        value = get_current_user(request)
        if hasattr(value, "__await__"):
            value = await value
        return value or {}

    @router.get(
        "/api/conversation-agents",
        response_model=ConversationAgentListResponse,
        operation_id="list_conversation_agents",
    )
    async def list_conversation_agents(
        session_id: Optional[str] = None,
        project_id: Optional[str] = None,
        limit: int = Query(default=100, ge=1, le=200),
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        user_info = await actor(request)
        user_id = str(user_info.get("id") or "").strip()
        if not user_id:
            raise HTTPException(status_code=401, detail="Not authenticated")
        try:
            agents = await ConversationAgentService(config=config).list_available_agents(
                actor_user_id=user_id,
                actor_role=user_info.get("role"),
                session_id=session_id,
                project_id=project_id,
                limit=limit,
            )
            return ConversationAgentListResponse(success=True, agents=agents)
        except ConversationAgentError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code},
            ) from exc

    @router.get(
        "/api/agents/{agent_id}/conversations",
        response_model=AgentConversationsResponse,
        operation_id="list_agent_conversations",
    )
    async def list_agent_conversations(
        agent_id: str,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        user_info = await actor(request)
        user_id = str(user_info.get("id") or "").strip()
        if not user_id:
            raise HTTPException(status_code=401, detail="Not authenticated")
        try:
            sessions = await ConversationAgentService(config=config).list_agent_conversations(
                agent_id=agent_id,
                actor_user_id=user_id,
                actor_role=user_info.get("role"),
                limit=limit,
                offset=offset,
            )
            return AgentConversationsResponse(
                success=True,
                sessions=sessions,
                total=len(sessions),
                limit=limit,
                offset=offset,
            )
        except ConversationAgentError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code},
            ) from exc

    return router
