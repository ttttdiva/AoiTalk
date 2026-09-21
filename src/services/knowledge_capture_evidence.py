"""Bounded, ACL-revalidated evidence for one completed Project Task.

This module owns retrieval only.  It never writes Tasks, Docs, files, Memory,
notifications, or candidate rows.  Source prose is clipped and tagged as
untrusted evidence before it can reach the curator contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import and_, or_, select

from ..memory.models import (
    AgentRun,
    ContextMemory,
    ConversationMessage,
    ConversationParticipant,
    ConversationSession,
    KnowledgeNode,
    KnowledgeRevision,
    Project,
    ProjectKnowledgeRef,
    Task,
    TaskActivity,
    TaskAttachment,
    TaskComment,
    TaskDependency,
    TaskReference,
    TaskRelation,
)
from .docs_acl import docs_readable_node_predicate
from .project_permissions import has_effective_project_permission
from .project_workspace_cleanup import get_project_workspace_path
from .privacy_masking_projection import is_privacy_masking_source


MAX_EVIDENCE_ITEMS = 160
MAX_GRAPH_DEPTH = 2
MAX_RELATED_TASKS = 48
MAX_CHAT_MESSAGES = 48
MAX_DOC_REVISIONS = 32
MAX_MEMORY_ROWS = 48
MAX_WORKSPACE_BYTES = 64 * 1024
MAX_WORKSPACE_SEARCH_FILES = 80
MAX_TEXT_CHARS = 4_000
MAX_METADATA_ITEMS = 32
CHAT_HEAD_MESSAGES = 12
CHAT_TAIL_MESSAGES = 36
REVIEW_CHAT_SESSION_LIMIT = 8

EVIDENCE_KINDS = frozenset(
    {
        "task",
        "task_activity",
        "task_comment",
        "task_attachment",
        "task_reference",
        "conversation_session",
        "conversation_message",
        "agent_run",
        "docs_node",
        "docs_revision",
        "project_memory",
        "workspace_file",
        "url",
        "user_confirmation",
    }
)
EVIDENCE_STRENGTHS = frozenset(
    {"authoritative", "strong", "explicit", "supporting", "advisory", "web", "weak"}
)
EVIDENCE_AUTHORSHIP = frozenset(
    {"user", "human", "canonical", "assistant", "system", "external", "web", "user_confirmation"}
)

_DRIVE_PATH = re.compile(r"^[A-Za-z]:($|[/\\])")
_SECRET_KEY_PARTS = (
    "secret",
    "token",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "private_key",
)


class EvidenceClusterError(RuntimeError):
    """Evidence cannot be read within the current live Project boundary."""


class EvidenceScopeError(EvidenceClusterError, PermissionError):
    """The actor or source is outside the bound Project scope."""


def _value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return getattr(row, key, default)
    except Exception:
        return default


def _uuid(value: Any, *, field_name: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise EvidenceClusterError(f"invalid {field_name}") from exc


def _maybe_uuid(value: Any) -> uuid.UUID | None:
    try:
        return _uuid(value, field_name="id")
    except EvidenceClusterError:
        return None


def _actor_id(actor: Any) -> uuid.UUID | None:
    if actor is None:
        return None
    if isinstance(actor, Mapping):
        for key in ("user_id", "actor_id", "id"):
            if actor.get(key) is not None:
                return _maybe_uuid(actor[key])
        return None
    for key in ("user_id", "actor_id", "id"):
        value = getattr(actor, key, None)
        if value is not None:
            return _maybe_uuid(value)
    return _maybe_uuid(actor)


def _actor_role(actor: Any) -> str:
    if isinstance(actor, Mapping):
        return str(actor.get("role") or actor.get("user_role") or "").strip().casefold()
    return str(getattr(actor, "role", getattr(actor, "user_role", "")) or "").strip().casefold()


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.isoformat()
    return str(value)


def _clip(value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _secret_key(value: Any) -> bool:
    normalized = str(value or "").strip().casefold()
    return any(part in normalized for part in _SECRET_KEY_PARTS)


def _bounded_json(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and value not in {float("inf"), float("-inf")} else None
    if isinstance(value, str):
        return _clip(value, 1_000)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(sorted(value.items(), key=lambda pair: str(pair[0]))):
            if index >= MAX_METADATA_ITEMS or _secret_key(key):
                continue
            result[str(key)[:120]] = _bounded_json(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_bounded_json(item, depth=depth + 1) for item in list(value)[:MAX_METADATA_ITEMS]]
    return _clip(value, 1_000)


def content_hash(value: Any) -> str:
    if isinstance(value, bytes):
        data = value
    elif isinstance(value, str):
        data = value.encode("utf-8", errors="replace")
    else:
        data = json.dumps(_bounded_json(value), ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def typed_evidence_id(kind: str, source_id: Any) -> str:
    normalized_kind = str(kind or "").strip().casefold()
    if normalized_kind not in EVIDENCE_KINDS:
        raise EvidenceClusterError("unsupported evidence kind")
    source = str(source_id or "").strip()
    if not source or any(char in source for char in "\r\n\x00"):
        raise EvidenceClusterError("invalid evidence source")
    # IDs are stable and human-auditable; paths/URLs use a digest so they can
    # never smuggle an absolute path or arbitrary URL into a model citation.
    if normalized_kind in {"workspace_file", "url"}:
        source = hashlib.sha256(source.encode("utf-8", errors="replace")).hexdigest()[:32]
    return f"{normalized_kind}:{source}"


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    id: str
    kind: str
    source_id: str
    project_id: str
    version: str | None
    content_hash: str
    relation: str
    strength: str
    authorship: str
    text: str = ""
    source_path: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in EVIDENCE_KINDS:
            raise ValueError("unsupported evidence kind")
        if self.strength not in EVIDENCE_STRENGTHS:
            raise ValueError("unsupported evidence strength")
        if self.authorship not in EVIDENCE_AUTHORSHIP:
            raise ValueError("unsupported evidence authorship")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "source_id": self.source_id,
            "source_path": self.source_path,
            "project_id": self.project_id,
            "version": self.version,
            "content_hash": self.content_hash,
            "relation": self.relation,
            "strength": self.strength,
            "authorship": self.authorship,
            "text": _clip(self.text),
            "metadata": _bounded_json(self.metadata),
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


@dataclass(frozen=True, slots=True)
class EvidenceCluster:
    project_id: str
    seed_task_id: str
    items: tuple[EvidenceItem, ...]

    @property
    def registry(self) -> dict[str, dict[str, Any]]:
        return {item.id: item.to_dict() for item in self.items}

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "seed_task_id": self.seed_task_id,
            "evidence": [item.to_dict() for item in self.items],
            "evidence_ids": list(self.evidence_ids),
        }


async def _execute(session: Any, statement: Any) -> Any:
    execute = getattr(session, "execute", None)
    if not callable(execute):
        return None
    return await execute(statement)


async def _scalars(session: Any, statement: Any, *, limit: int | None = None) -> list[Any]:
    try:
        result = await _execute(session, statement)
        if result is None:
            return []
        scalars = getattr(result, "scalars", None)
        if callable(scalars):
            values = scalars()
            all_values = getattr(values, "all", None)
            if callable(all_values):
                rows = list(all_values())
            else:
                rows = list(values or [])
        else:
            rows = list(result or [])
        return rows[:limit] if limit is not None else rows
    except Exception:
        return []


async def _first(session: Any, statement: Any) -> Any:
    values = await _scalars(session, statement, limit=1)
    return values[0] if values else None


async def _permission(session: Any, project: Any, actor: Any) -> bool:
    actor_uuid = _actor_id(actor)
    if actor_uuid is None:
        return False
    if project is None or _value(project, "deleted_at") is not None:
        return False
    try:
        from ..memory.project_repository import ProjectRepository

        return bool(await ProjectRepository.has_permission(session, _uuid(_value(project, "id"), field_name="project_id"), actor_uuid, "read"))
    except Exception:
        if _value(project, "owner_id") is not None and str(_value(project, "owner_id")) == str(actor_uuid):
            return True
        if _actor_role(actor) == "admin":
            return True
        for member in _value(project, "members", ()) or ():
            if str(_value(member, "user_id")) == str(actor_uuid):
                return has_effective_project_permission(
                    user_id=actor_uuid,
                    user_role=_actor_role(actor),
                    project_owner_id=_value(project, "owner_id"),
                    member_permissions=_value(member, "permissions"),
                    permission="read",
                )
        return False


def _new_item(
    *,
    kind: str,
    source_id: Any,
    project_id: Any,
    relation: str,
    strength: str,
    authorship: str,
    text: Any = "",
    version: Any = None,
    source_path: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> EvidenceItem:
    safe_text = _clip(text)
    return EvidenceItem(
        id=typed_evidence_id(kind, source_id),
        kind=kind,
        source_id=str(source_id),
        project_id=str(project_id),
        version=_iso(version),
        content_hash=content_hash(safe_text or metadata or source_id),
        relation=_clip(relation, 160),
        strength=str(strength),
        authorship=str(authorship),
        text=safe_text,
        source_path=source_path,
        metadata=dict(metadata or {}),
    )


def _confirmation_rows(value: Any) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def build_user_confirmation_evidence(
    *,
    candidate_id: Any,
    question_id: Any,
    project_id: Any,
    answer: Any,
    actor_id: Any,
    answered_at: Any = None,
    option_id: Any = None,
) -> EvidenceItem:
    """Create the strong, typed evidence row for an authorized user answer."""

    source_id = f"{candidate_id}:{question_id}"
    return _new_item(
        kind="user_confirmation",
        source_id=source_id,
        project_id=project_id,
        relation="blocking_question_answer",
        strength="authoritative",
        authorship="user_confirmation",
        text=answer,
        version=answered_at,
        metadata={"candidate_id": str(candidate_id), "question_id": str(question_id), "actor_id": str(actor_id), "option_id": str(option_id) if option_id else None},
    )


def _task_text(task: Any) -> str:
    return "\n".join(
        value
        for value in (
            _clip(_value(task, "title"), 800),
            _clip(_value(task, "description"), 2_500),
            f"status={_value(task, 'status', '')}",
            f"priority={_value(task, 'priority', '')}",
        )
        if value
    )


def evidence_text_hash(text: Any) -> str:
    return content_hash(_clip(text))


def task_activity_evidence_text(row: Any) -> str:
    payload = _bounded_json(_value(row, "payload", {}) or {})
    return f"{_value(row, 'activity_type', '')}\n{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"


def task_attachment_evidence_text(row: Any) -> str:
    return f"{_value(row, 'display_name', '')} ({_value(row, 'mime_type', '')})"


def conversation_session_evidence_text(row: Any) -> str:
    return f"{_value(row, 'title', '')}\n{_value(row, 'character_name', '')}"


def agent_run_evidence_text(row: Any) -> str:
    return f"{_value(row, 'title', '')}\n{_value(row, 'objective', '')}"


def docs_node_evidence_text(row: Any) -> str:
    return f"{_value(row, 'title', '')}\n{_value(row, 'body_text', '')}"


def docs_revision_evidence_text(row: Any) -> str:
    return f"{_value(row, 'title', '')}\n{_value(row, 'body_text', '')}\n{_value(row, 'change_summary', '')}"


def project_memory_evidence_text(row: Any) -> str:
    return f"{_value(row, 'title', '')}\n{_value(row, 'content', '')}"


def workspace_file_version(info: Mapping[str, Any] | None) -> str:
    payload = info if isinstance(info, Mapping) else {}
    return f"{payload.get('mtime_ns')}:{payload.get('size_bytes')}"


def _task_authorship(task: Any) -> str:
    return "user" if _value(task, "created_by") is not None else "canonical"


def _safe_url(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw or len(raw) > 2_000 or "\x00" in raw:
        return None
    from urllib.parse import urlsplit, urlunsplit

    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        return None
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc, parsed.path, parsed.query, ""))


def _relative_workspace_path(value: Any, project_id: Any) -> str:
    raw = str(value or "").replace("\\", "/").strip()
    if not raw or "\x00" in raw or _DRIVE_PATH.match(raw) or raw.startswith(("/", "//")):
        raise ValueError("workspace path must be relative")
    prefix = f"_projects/project_{project_id}/"
    if raw.casefold().startswith(prefix.casefold()):
        raw = raw[len(prefix) :]
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("workspace path escaped Project")
    return "/".join(parts)


def resolve_project_workspace_file(
    project_id: Any,
    relative_path: Any,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
) -> tuple[Path, str]:
    """Resolve a Project-relative file and fail closed on traversal/reparse."""

    project_uuid = _uuid(project_id, field_name="project_id")
    relative = _relative_workspace_path(relative_path, project_uuid)
    root = get_project_workspace_path(project_uuid, workspace_root=workspace_root).resolve()
    target = (root / Path(relative)).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("workspace path escaped Project") from exc
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("reparse/symlink workspace path is not readable")
    if not target.is_file():
        raise FileNotFoundError(relative)
    return target, relative


def read_bound_project_file(
    project_id: Any,
    relative_path: Any,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
    max_bytes: int = MAX_WORKSPACE_BYTES,
) -> dict[str, Any]:
    path, relative = resolve_project_workspace_file(project_id, relative_path, workspace_root=workspace_root)
    stat = path.stat()
    data = path.read_bytes()[: max(1, min(int(max_bytes), MAX_WORKSPACE_BYTES))]
    text = data.decode("utf-8", errors="replace")
    return {
        "path": relative,
        "size_bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "sha256": hashlib.sha256(data).hexdigest(),
        "excerpt": _clip(text, 8_000),
    }


def search_bound_project_files(
    project_id: Any,
    query: Any,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    project_uuid = _uuid(project_id, field_name="project_id")
    terms = [part.casefold() for part in str(query or "").split() if part.strip()]
    if not terms:
        return []
    root = get_project_workspace_path(project_uuid, workspace_root=workspace_root).resolve()
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if len(rows) >= min(max(int(limit), 1), MAX_WORKSPACE_SEARCH_FILES) or not path.is_file() or path.is_symlink():
            continue
        try:
            relative = path.resolve().relative_to(root).as_posix()
            data = path.read_bytes()[:MAX_WORKSPACE_BYTES]
        except (OSError, ValueError):
            continue
        text = data.decode("utf-8", errors="replace")
        folded = text.casefold()
        if all(term in folded for term in terms):
            stat = path.stat()
            rows.append(
                {
                    "path": relative,
                    "size_bytes": int(stat.st_size),
                    "mtime_ns": int(stat.st_mtime_ns),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "excerpt": _clip(text, 2_000),
                }
            )
    return rows


def _merge_explicit_chat_window(
    head_messages: Sequence[Any],
    tail_messages_desc: Sequence[Any],
    direct_messages: Sequence[Any],
) -> list[Any]:
    """Return a de-duplicated chronological head + tail + direct window."""

    merged: list[Any] = []
    seen: set[str] = set()
    for message in [
        *list(head_messages),
        *list(reversed(list(tail_messages_desc))),
        *list(direct_messages),
    ]:
        message_id = str(_value(message, "id") or "")
        if not message_id or message_id in seen:
            continue
        seen.add(message_id)
        merged.append(message)
    merged.sort(
        key=lambda row: (
            _iso(_value(row, "created_at")) or "",
            str(_value(row, "id") or ""),
        )
    )
    return merged


def _chat_window_limits(session_count: int) -> tuple[tuple[int, int], ...]:
    """Return bounded head/tail limits for each visible linked chat session."""

    count = min(max(int(session_count), 0), MAX_CHAT_MESSAGES)
    if count == 0:
        return ()
    if count == 1:
        return ((CHAT_HEAD_MESSAGES, CHAT_TAIL_MESSAGES),)
    if count <= 24:
        per_session = MAX_CHAT_MESSAGES // count
        head_limit = max(1, min(CHAT_HEAD_MESSAGES, per_session // 4))
        tail_limit = max(1, per_session - head_limit)
        while count * (head_limit + tail_limit) > MAX_CHAT_MESSAGES:
            if tail_limit > 1:
                tail_limit -= 1
            elif head_limit > 1:
                head_limit -= 1
            else:
                break
        return tuple((head_limit, tail_limit) for _ in range(count))
    return tuple((0, 1) for _ in range(count))


def _explicit_chat_session_budget(session_count: int) -> tuple[int, int]:
    """Return the ZIP-compatible budget for the first visible session."""

    limits = _chat_window_limits(session_count)
    return limits[0] if limits else (0, 0)


def _chat_session_order_key(row: Any) -> tuple[str, str, str]:
    return (
        _iso(_value(row, "last_activity")) or _iso(_value(row, "session_start")) or "",
        _iso(_value(row, "session_start")) or "",
        str(_value(row, "id") or ""),
    )


def _chat_session_visible_to_actor(
    row: Any,
    *,
    actor_text: str,
    participant_session_ids: set[str],
) -> bool:
    """Require the live actor to own or have joined the conversation session."""

    if not actor_text:
        return False
    session_id = str(_value(row, "id") or "")
    return (
        str(_value(row, "user_id") or "") == actor_text
        or session_id in participant_session_ids
    )


def _ordered_visible_chat_sessions(
    sessions: Sequence[Any],
    *,
    actor_text: str,
    participant_session_ids: set[str],
) -> list[Any]:
    """Apply the existing actor ACL, then impose a stable visible order."""

    visible: list[Any] = []
    seen_ids: set[str] = set()
    for row in sessions:
        session_id = str(_value(row, "id") or "")
        if not session_id or session_id in seen_ids:
            continue
        if not _chat_session_visible_to_actor(
            row,
            actor_text=actor_text,
            participant_session_ids=participant_session_ids,
        ):
            continue
        seen_ids.add(session_id)
        visible.append(row)
    visible.sort(key=_chat_session_order_key)
    return visible


def _chat_reference_ids(row: Any) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """Resolve a chat reference to ``(session_id, message_id)``.

    The task-reference API stores a message reference's session in
    ``target_id`` and the actual message in ``reference_metadata.message_id``.
    Older rows used ``target_id`` as the message ID, so retain that fallback.
    """

    reference_type = str(_value(row, "reference_type", "")).strip().casefold()
    target_id = _maybe_uuid(_value(row, "target_id"))
    if reference_type == "conversation_session":
        return target_id, None
    if reference_type != "conversation_message":
        return None, None
    metadata = _value(row, "reference_metadata", None)
    if not isinstance(metadata, Mapping):
        metadata = _value(row, "metadata", {})
    message_id = _maybe_uuid(metadata.get("message_id")) if isinstance(metadata, Mapping) else None
    if message_id is not None:
        return target_id, message_id
    return None, target_id


async def resolve_knowledge_capture_review_chat_sessions(
    session: Any,
    *,
    task_id: Any,
    project_id: Any,
    actor: Any,
    evidence_refs: Sequence[Mapping[str, Any]] = (),
    limit: int = REVIEW_CHAT_SESSION_LIMIT,
) -> list[dict[str, Any]]:
    """Resolve bounded, live, actor-visible chat provenance for review UI."""

    task_uuid = _uuid(task_id, field_name="task_id")
    project_uuid = _uuid(project_id, field_name="project_id")
    actor_uuid = _actor_id(actor)
    if actor_uuid is None:
        return []

    try:
        bounded_limit = max(0, min(int(limit), REVIEW_CHAT_SESSION_LIMIT))
    except (TypeError, ValueError):
        bounded_limit = REVIEW_CHAT_SESSION_LIMIT
    if bounded_limit == 0:
        return []

    scan_limit = REVIEW_CHAT_SESSION_LIMIT * 4
    actor_text = str(actor_uuid)

    async def visible_sessions(
        session_ids: set[uuid.UUID],
        message_sessions: dict[uuid.UUID, set[uuid.UUID] | None],
    ) -> list[Any]:
        resolved_ids = set(session_ids)
        if message_sessions:
            messages = await _scalars(
                session,
                select(ConversationMessage)
                .join(ConversationSession, ConversationSession.id == ConversationMessage.session_id)
                .where(
                    ConversationMessage.id.in_(sorted(message_sessions, key=str)),
                    ConversationMessage.deleted_at.is_(None),
                    ConversationSession.project_id == project_uuid,
                    ConversationSession.deleted_at.is_(None),
                )
                .order_by(ConversationMessage.created_at.asc(), ConversationMessage.id.asc())
                .limit(scan_limit),
                limit=scan_limit,
            )
            for message in messages:
                message_id = _maybe_uuid(_value(message, "id"))
                live_session_id = _maybe_uuid(_value(message, "session_id"))
                if message_id is None or live_session_id is None:
                    continue
                expected = message_sessions.get(message_id)
                if expected is not None and live_session_id not in expected:
                    continue
                resolved_ids.add(live_session_id)

        if not resolved_ids:
            return []
        conversations = await _scalars(
            session,
            select(ConversationSession)
            .where(
                ConversationSession.id.in_(sorted(resolved_ids, key=str)),
                ConversationSession.project_id == project_uuid,
                ConversationSession.deleted_at.is_(None),
            )
            .order_by(ConversationSession.id.asc())
            .limit(scan_limit),
            limit=scan_limit,
        )
        if not conversations:
            return []
        conversation_ids = [
            _uuid(_value(row, "id"), field_name="session_id") for row in conversations
        ]
        participants = await _scalars(
            session,
            select(ConversationParticipant)
            .where(
                ConversationParticipant.session_id.in_(conversation_ids),
                ConversationParticipant.participant_type == "user",
                ConversationParticipant.participant_id == actor_text,
                ConversationParticipant.status == "joined",
            )
            .limit(scan_limit),
            limit=scan_limit,
        )
        participant_session_ids = {
            str(_value(row, "session_id")) for row in participants
        }
        return _ordered_visible_chat_sessions(
            conversations,
            actor_text=actor_text,
            participant_session_ids=participant_session_ids,
        )

    def add_message(
        target: dict[uuid.UUID, set[uuid.UUID] | None],
        message_id: uuid.UUID,
        expected_session_id: uuid.UUID | None,
    ) -> None:
        if expected_session_id is None:
            target[message_id] = None
            return
        current = target.get(message_id)
        if message_id not in target:
            target[message_id] = {expected_session_id}
        elif current is not None:
            current.add(expected_session_id)

    references = await _scalars(
        session,
        select(TaskReference)
        .where(
            TaskReference.task_id == task_uuid,
            TaskReference.project_id == project_uuid,
            TaskReference.reference_type.in_(("conversation_session", "conversation_message")),
        )
        .order_by(TaskReference.created_at.asc(), TaskReference.id.asc())
        .limit(scan_limit),
        limit=scan_limit,
    )
    direct_session_ids: set[uuid.UUID] = set()
    direct_messages: dict[uuid.UUID, set[uuid.UUID] | None] = {}
    for reference in references:
        reference_session_id, reference_message_id = _chat_reference_ids(reference)
        if reference_message_id is not None:
            add_message(direct_messages, reference_message_id, reference_session_id)
        elif reference_session_id is not None:
            direct_session_ids.add(reference_session_id)

    direct = await visible_sessions(direct_session_ids, direct_messages)
    if direct:
        single = len(direct) == 1
        return [
            {
                "id": str(_value(row, "id")),
                "title": (
                    " ".join(
                        str(_value(row, "title", "") or "")
                        .replace("\r", " ")
                        .replace("\n", " ")
                        .split()
                    )[:240]
                    or None
                ),
                "relation": "direct_reference",
                "primary": single,
            }
            for row in direct[:bounded_limit]
        ]

    supporting_session_ids: set[uuid.UUID] = set()
    supporting_messages: dict[uuid.UUID, set[uuid.UUID] | None] = {}
    for evidence in list(evidence_refs)[:scan_limit]:
        if not isinstance(evidence, Mapping):
            continue
        kind = str(evidence.get("type") or "").strip().casefold()
        source_value = evidence.get("source_id") or evidence.get("id")
        source_text = str(source_value or "").strip()
        typed_prefix = f"{kind}:"
        if source_text.casefold().startswith(typed_prefix):
            source_text = source_text[len(typed_prefix) :]
        source_id = _maybe_uuid(source_text)
        if source_id is None:
            continue
        if kind == "conversation_session":
            supporting_session_ids.add(source_id)
        elif kind == "conversation_message":
            add_message(supporting_messages, source_id, None)

    supporting = await visible_sessions(supporting_session_ids, supporting_messages)
    return [
        {
            "id": str(_value(row, "id")),
            "title": (
                " ".join(
                    str(_value(row, "title", "") or "")
                    .replace("\r", " ")
                    .replace("\n", " ")
                    .split()
                )[:240]
                or None
            ),
            "relation": "supporting_evidence",
            "primary": False,
        }
        for row in supporting[:bounded_limit]
    ]


async def resolve_knowledge_capture_review_publication_target(
    session: Any,
    *,
    project_id: Any,
    actor: Any,
    publication: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Resolve one live, readable Docs publication target for review UI."""

    if not isinstance(publication, Mapping):
        return None
    action = str(publication.get("action") or "").strip().casefold()
    if action not in {"create", "update", "no_change"}:
        return None

    hidden = {"action": action, "node_id": None, "title": None}
    if action != "update":
        return hidden

    project_uuid = _uuid(project_id, field_name="project_id")
    actor_uuid = _actor_id(actor)
    target_uuid = _maybe_uuid(publication.get("target_node_id"))
    if actor_uuid is None or target_uuid is None:
        return hidden

    node = await _first(
        session,
        select(KnowledgeNode).where(
            KnowledgeNode.id == target_uuid,
            KnowledgeNode.archived_at.is_(None),
        ),
    )
    if node is None:
        return hidden

    raw_node_project_id = _value(node, "project_id")
    if raw_node_project_id is not None:
        node_project_id = _maybe_uuid(raw_node_project_id)
        if node_project_id != project_uuid:
            return hidden
        project = await _first(
            session,
            select(Project).where(
                Project.id == project_uuid,
                Project.deleted_at.is_(None),
            ),
        )
        if project is None or not await _permission(session, project, actor):
            return hidden
        authorized_node = node
    else:
        try:
            predicate = docs_readable_node_predicate(
                KnowledgeNode,
                docs_library_id=_value(node, "docs_library_id"),
                user_id=actor_uuid,
                required="read",
            )
            authorized_node = await _first(
                session,
                select(KnowledgeNode).where(
                    KnowledgeNode.id == target_uuid,
                    KnowledgeNode.archived_at.is_(None),
                    KnowledgeNode.project_id.is_(None),
                    predicate,
                ),
            )
        except Exception:
            return hidden
        if authorized_node is None:
            return hidden

    title = (
        " ".join(
            str(_value(authorized_node, "title", "") or "")
            .replace("\r", " ")
            .replace("\n", " ")
            .split()
        )[:240]
        or None
    )
    return {
        "action": "update",
        "node_id": str(target_uuid),
        "title": title,
    }


def _direct_message_evidence_ids(messages: Sequence[Any]) -> set[str]:
    return {
        typed_evidence_id("conversation_message", message_id)
        for message_id in (str(_value(row, "id") or "") for row in messages)
        if message_id
    }


def _deduplicate_chat_messages(
    messages: Sequence[Any],
    *,
    mandatory_direct_ids: set[str],
) -> list[tuple[Any, bool]]:
    """Keep the first message object and retain whether it is a direct ref."""

    seen_message_ids: set[str] = set()
    unique: list[tuple[Any, bool]] = []
    for message in messages:
        message_key = str(_value(message, "id") or "")
        if not message_key or message_key in seen_message_ids:
            continue
        seen_message_ids.add(message_key)
        evidence_id = typed_evidence_id("conversation_message", message_key)
        unique.append((message, evidence_id in mandatory_direct_ids))
    return unique


def _merge_allocated_chat_windows(
    session_windows: Sequence[tuple[Any, Sequence[Any], Sequence[Any]]],
    direct_messages: Sequence[Any],
) -> list[Any]:
    """Merge per-session head/tail query results with mandatory direct refs."""

    limits = _chat_window_limits(len(session_windows))
    head_messages: list[Any] = []
    tail_messages_desc: list[Any] = []
    for (_session_id, head_rows, tail_rows_desc), (head_limit, tail_limit) in zip(
        session_windows, limits
    ):
        head_messages.extend(list(head_rows)[:head_limit])
        tail_messages_desc.extend(list(tail_rows_desc)[:tail_limit])
    return _merge_explicit_chat_window(head_messages, tail_messages_desc, direct_messages)


class KnowledgeCaptureEvidenceService:
    """Load one deterministic Task-centered evidence cluster."""

    def __init__(
        self,
        session: Any,
        *,
        workspace_root: str | os.PathLike[str] | None = None,
        max_depth: int = MAX_GRAPH_DEPTH,
        max_items: int = MAX_EVIDENCE_ITEMS,
    ) -> None:
        self.session = session
        self.workspace_root = workspace_root
        self.max_depth = min(max(int(max_depth), 0), MAX_GRAPH_DEPTH)
        self.max_items = min(max(int(max_items), 1), MAX_EVIDENCE_ITEMS)
        self._items: dict[str, EvidenceItem] = {}
        self._mandatory_evidence_ids: set[str] = set()
        self._project_id = ""
        self._project: Any = None
        self._actor: Any = None

    def _add(self, item: EvidenceItem, *, mandatory: bool = False) -> None:
        if mandatory:
            self._mandatory_evidence_ids.add(item.id)
        if item.id in self._items:
            return
        if len(self._items) >= self.max_items:
            if not mandatory:
                return
            evict_id = next(
                (
                    evidence_id
                    for evidence_id in self._items
                    if evidence_id not in self._mandatory_evidence_ids
                ),
                None,
            )
            if evict_id is None:
                # Direct references are explicit contract inputs.  Keep them
                # rather than silently dropping a late reference.
                self._items[item.id] = item
                return
            del self._items[evict_id]
        self._items.setdefault(item.id, item)

    async def _load_seed(self, seed_task: Any, project_id: Any | None) -> Any:
        if isinstance(seed_task, (Task, Mapping)) or hasattr(seed_task, "project_id"):
            task = seed_task
        else:
            task_uuid = _uuid(seed_task, field_name="seed_task_id")
            task = await _first(
                self.session,
                select(Task).where(Task.id == task_uuid, Task.deleted_at.is_(None)),
            )
        if task is None or _value(task, "deleted_at") is not None:
            raise EvidenceClusterError("seed Task is unavailable")
        resolved_project = _value(task, "project_id") or project_id
        if resolved_project is None:
            raise EvidenceScopeError("seed Task is projectless")
        self._project_id = str(resolved_project)
        project = await _first(
            self.session,
            select(Project).where(Project.id == _uuid(self._project_id, field_name="project_id"), Project.deleted_at.is_(None)),
        )
        if project is None:
            project = _value(task, "project")
        if not await _permission(self.session, project, self._actor):
            raise EvidenceScopeError("Project read access denied")
        self._project = project
        if str(_value(task, "project_id")) != self._project_id:
            raise EvidenceScopeError("seed Task Project binding changed")
        return task

    async def collect(
        self,
        seed_task: Any,
        actor: Any,
        *,
        project_id: Any | None = None,
        user_confirmation: Mapping[str, Any] | None = None,
    ) -> EvidenceCluster:
        self._actor = actor
        self._items.clear()
        self._mandatory_evidence_ids.clear()
        task = await self._load_seed(seed_task, project_id)
        seed_id = str(_value(task, "id"))
        self._add(
            _new_item(
                kind="task",
                source_id=seed_id,
                project_id=self._project_id,
                relation="seed",
                strength="authoritative",
                authorship=_task_authorship(task),
                text=_task_text(task),
                version=_value(task, "updated_at") or _value(task, "completed_at"),
                metadata={
                    "status": _value(task, "status"),
                    "completed_at": _iso(_value(task, "completed_at")),
                    "knowledge_node_id": str(_value(task, "knowledge_node_id")) if _value(task, "knowledge_node_id") else None,
                },
            )
        )
        task_rows, edge_relations = await self._expand_tasks(task)
        task_ids = {str(_value(row, "id")) for row in task_rows if _value(row, "id") is not None}
        task_ids.add(seed_id)
        for row in task_rows:
            row_id = str(_value(row, "id"))
            self._add(
                _new_item(
                    kind="task",
                    source_id=row_id,
                    project_id=self._project_id,
                    relation=edge_relations.get(row_id, "related_task"),
                    strength="strong",
                    authorship=_task_authorship(row),
                    text=_task_text(row),
                    version=_value(row, "updated_at") or _value(row, "completed_at"),
                    metadata={"status": _value(row, "status"), "parent_task_id": str(_value(row, "parent_task_id")) if _value(row, "parent_task_id") else None},
                )
            )
        await self._collect_task_rows(task_ids)
        await self._collect_agent_runs(task_ids)
        await self._collect_docs(task, task_ids)
        await self._collect_memories(task_ids)
        await self._collect_explicit_files(task, task_ids)
        for confirmation in _confirmation_rows(user_confirmation):
            answer = confirmation.get("answer") or confirmation.get("text")
            if not str(answer or "").strip():
                continue
            question_id = confirmation.get("id") or confirmation.get("question_id") or "confirmation"
            candidate_id = confirmation.get("candidate_id") or "candidate"
            self._add(
                build_user_confirmation_evidence(
                    candidate_id=candidate_id,
                    question_id=question_id,
                    project_id=self._project_id,
                    answer=answer,
                    actor_id=confirmation.get("actor_id") or _actor_id(self._actor) or "",
                    answered_at=confirmation.get("answered_at") or confirmation.get("version"),
                    option_id=confirmation.get("option_id"),
                )
            )
        return EvidenceCluster(
            project_id=self._project_id,
            seed_task_id=seed_id,
            items=tuple(self._items.values()),
        )

    async def build_cluster(self, *args: Any, **kwargs: Any) -> EvidenceCluster:
        return await self.collect(*args, **kwargs)

    async def _conversation_visible_to_actor(self, conversation: Any) -> bool:
        actor_text = str(_actor_id(self._actor) or "")
        if _chat_session_visible_to_actor(
            conversation,
            actor_text=actor_text,
            participant_session_ids=set(),
        ):
            return True
        if not actor_text:
            return False
        participant = await _first(
            self.session,
            select(ConversationParticipant).where(
                ConversationParticipant.session_id == _value(conversation, "id"),
                ConversationParticipant.participant_type == "user",
                ConversationParticipant.participant_id == actor_text,
                ConversationParticipant.status == "joined",
            ),
        )
        return _chat_session_visible_to_actor(
            conversation,
            actor_text=actor_text,
            participant_session_ids={
                str(_value(participant, "session_id"))
            }
            if participant is not None
            else set(),
        )

    async def revalidate(self, cluster: EvidenceCluster, actor: Any | None = None) -> EvidenceCluster:
        """Re-read live bindings immediately before model use/publication.

        Deleted, masked, rebound, or hash-changed sources are omitted.  The
        result is intentionally a new bounded registry; callers must never
        keep citing an old cluster after this check.
        """

        if actor is not None:
            self._actor = actor
        self._project_id = str(cluster.project_id)
        project = await _first(
            self.session,
            select(Project).where(
                Project.id == _uuid(self._project_id, field_name="project_id"),
                Project.deleted_at.is_(None),
            ),
        )
        if project is None or not await _permission(self.session, project, self._actor):
            raise EvidenceScopeError("Project ACL changed")
        kept: list[EvidenceItem] = []
        for item in cluster.items:
            source_uuid = _maybe_uuid(item.source_id)
            row = None
            if item.kind == "task" and source_uuid:
                row = await _first(self.session, select(Task).where(Task.id == source_uuid, Task.project_id == project.id, Task.deleted_at.is_(None)))
            elif item.kind == "task_activity" and source_uuid:
                row = await _first(self.session, select(TaskActivity).join(Task, Task.id == TaskActivity.task_id).where(TaskActivity.id == source_uuid, Task.project_id == project.id, Task.deleted_at.is_(None)))
            elif item.kind == "task_comment" and source_uuid:
                row = await _first(self.session, select(TaskComment).join(Task, Task.id == TaskComment.task_id).where(TaskComment.id == source_uuid, Task.project_id == project.id, Task.deleted_at.is_(None)))
            elif item.kind in {"task_attachment", "task_reference"} and source_uuid:
                model = TaskAttachment if item.kind == "task_attachment" else TaskReference
                row = await _first(self.session, select(model).where(model.id == source_uuid, model.project_id == project.id))
            elif item.kind == "conversation_session" and source_uuid:
                row = await _first(self.session, select(ConversationSession).where(ConversationSession.id == source_uuid, ConversationSession.project_id == project.id, ConversationSession.deleted_at.is_(None)))
                if row is not None and not await self._conversation_visible_to_actor(row):
                    row = None
            elif item.kind == "conversation_message" and source_uuid:
                row = await _first(self.session, select(ConversationMessage).join(ConversationSession, ConversationSession.id == ConversationMessage.session_id).where(ConversationMessage.id == source_uuid, ConversationMessage.deleted_at.is_(None), ConversationSession.project_id == project.id, ConversationSession.deleted_at.is_(None)))
                if row is not None:
                    conversation = await _first(
                        self.session,
                        select(ConversationSession).where(
                            ConversationSession.id == _value(row, "session_id"),
                            ConversationSession.project_id == project.id,
                            ConversationSession.deleted_at.is_(None),
                        ),
                    )
                    if conversation is None or not await self._conversation_visible_to_actor(conversation):
                        row = None
                if row is not None and is_privacy_masking_source(row):
                    row = None
            elif item.kind == "agent_run" and source_uuid:
                row = await _first(self.session, select(AgentRun).where(AgentRun.id == source_uuid, AgentRun.project_id == project.id, AgentRun.task_id.is_not(None)))
            elif item.kind == "docs_node" and source_uuid:
                row = await _first(self.session, select(KnowledgeNode).where(KnowledgeNode.id == source_uuid, KnowledgeNode.archived_at.is_(None), or_(KnowledgeNode.project_id == project.id, KnowledgeNode.project_id.is_(None))))
                if row is not None and _value(row, "project_id") is None:
                    try:
                        predicate = docs_readable_node_predicate(KnowledgeNode, docs_library_id=_value(row, "docs_library_id"), user_id=_actor_id(self._actor), required="read")
                        row = await _first(self.session, select(KnowledgeNode).where(KnowledgeNode.id == source_uuid, KnowledgeNode.archived_at.is_(None), predicate))
                    except Exception:
                        row = None
            elif item.kind == "docs_revision" and source_uuid:
                row = await _first(self.session, select(KnowledgeRevision).join(KnowledgeNode, KnowledgeNode.id == KnowledgeRevision.node_id).where(KnowledgeRevision.id == source_uuid, KnowledgeNode.archived_at.is_(None), KnowledgeNode.project_id == project.id))
            elif item.kind == "project_memory" and source_uuid:
                row = await _first(self.session, select(ContextMemory).where(ContextMemory.id == source_uuid, ContextMemory.project_id == project.id, ContextMemory.status == "active"))
            elif item.kind == "workspace_file" and item.source_path:
                try:
                    info = read_bound_project_file(self._project_id, item.source_path, workspace_root=self.workspace_root)
                    expected_hash = item.metadata.get("sha256") if isinstance(item.metadata, Mapping) else None
                    if expected_hash and expected_hash != info.get("sha256"):
                        continue
                    row = info
                except (ValueError, FileNotFoundError, OSError):
                    row = None
            elif item.kind in {"url", "user_confirmation"}:
                row = True
            if row is not None:
                kept.append(item)
        return EvidenceCluster(project_id=self._project_id, seed_task_id=cluster.seed_task_id, items=tuple(kept))

    async def _expand_tasks(self, seed: Any) -> tuple[list[Any], dict[str, str]]:
        frontier = [seed]
        seen = {str(_value(seed, "id"))}
        rows: list[Any] = []
        relations: dict[str, str] = {}
        for depth in range(1, self.max_depth + 1):
            frontier_ids = [_uuid(_value(row, "id"), field_name="task_id") for row in frontier]
            if not frontier_ids:
                break
            neighbors: dict[str, str] = {}
            children = await _scalars(
                self.session,
                select(Task).where(
                    Task.project_id == _uuid(self._project_id, field_name="project_id"),
                    Task.parent_task_id.in_(frontier_ids),
                    Task.deleted_at.is_(None),
                ).limit(MAX_RELATED_TASKS),
            )
            for row in children:
                neighbors[str(_value(row, "id"))] = "subtask"
            parent_ids = [
                _maybe_uuid(_value(row, "parent_task_id"))
                for row in frontier
                if _value(row, "parent_task_id") is not None
            ]
            if parent_ids:
                parents = await _scalars(
                    self.session,
                    select(Task).where(
                        Task.project_id == _uuid(self._project_id, field_name="project_id"),
                        Task.id.in_(parent_ids),
                        Task.deleted_at.is_(None),
                    ).limit(MAX_RELATED_TASKS),
                )
                for row in parents:
                    neighbors[str(_value(row, "id"))] = "parent"
            relations_rows = await _scalars(
                self.session,
                select(TaskRelation).where(
                    or_(TaskRelation.task_a_id.in_(frontier_ids), TaskRelation.task_b_id.in_(frontier_ids))
                ).limit(MAX_RELATED_TASKS),
            )
            for relation in relations_rows:
                a, b = _value(relation, "task_a_id"), _value(relation, "task_b_id")
                if str(a) in {str(item) for item in frontier_ids}:
                    neighbors[str(b)] = f"relation:{_value(relation, 'relation_type', 'related')}"
                else:
                    neighbors[str(a)] = f"relation:{_value(relation, 'relation_type', 'related')}"
            dependencies = await _scalars(
                self.session,
                select(TaskDependency).where(
                    or_(TaskDependency.task_id.in_(frontier_ids), TaskDependency.depends_on_task_id.in_(frontier_ids))
                ).limit(MAX_RELATED_TASKS),
            )
            for dependency in dependencies:
                task_id = str(_value(dependency, "task_id"))
                depends_on = str(_value(dependency, "depends_on_task_id"))
                neighbors[depends_on if task_id in {str(item) for item in frontier_ids} else task_id] = "dependency"
            # Lightweight objects used by worker tests may expose relations
            # without a SQL session.  They remain subject to the same Project
            # binding check before entering the cluster.
            for row in frontier:
                for attr, relation_name in (("parent_task", "parent"), ("subtasks", "subtask"), ("children", "subtask"), ("related_tasks", "relation:related"), ("dependencies", "dependency")):
                    values = _value(row, attr, ()) or ()
                    if not isinstance(values, (list, tuple, set, frozenset)):
                        values = [values]
                    for candidate in values:
                        candidate_id = _value(candidate, "id")
                        if candidate_id is not None and str(_value(candidate, "project_id")) == self._project_id:
                            neighbors.setdefault(str(candidate_id), relation_name)
            neighbor_ids = [
                _maybe_uuid(identifier)
                for identifier in neighbors
                if identifier not in seen and _maybe_uuid(identifier) is not None
            ]
            if not neighbor_ids:
                break
            neighbor_rows = await _scalars(
                self.session,
                select(Task).where(
                    Task.project_id == _uuid(self._project_id, field_name="project_id"),
                    Task.id.in_(neighbor_ids),
                    Task.deleted_at.is_(None),
                ).limit(MAX_RELATED_TASKS),
            )
            by_id = {str(_value(row, "id")): row for row in neighbor_rows}
            next_frontier: list[Any] = []
            for identifier in neighbor_ids:
                row = by_id.get(str(identifier))
                if row is None:
                    continue
                row_id = str(_value(row, "id"))
                if row_id in seen:
                    continue
                seen.add(row_id)
                relations[row_id] = neighbors.get(row_id, f"related_depth_{depth}")
                rows.append(row)
                next_frontier.append(row)
                if len(rows) >= MAX_RELATED_TASKS:
                    return rows, relations
            frontier = next_frontier
            if not frontier:
                break
        return rows, relations

    async def _collect_task_rows(self, task_ids: set[str]) -> None:
        ids = [_maybe_uuid(value) for value in task_ids if _maybe_uuid(value) is not None]
        if not ids:
            return
        activities = await _scalars(
            self.session,
            select(TaskActivity).where(TaskActivity.task_id.in_(ids)).order_by(TaskActivity.created_at.asc()).limit(MAX_EVIDENCE_ITEMS),
        )
        for row in activities:
            if _value(row, "created_at") is None:
                continue
            self._add(
                _new_item(
                    kind="task_activity",
                    source_id=_value(row, "id"),
                    project_id=self._project_id,
                    relation="task_activity",
                    strength="supporting",
                    authorship="user" if _value(row, "user_id") else "system",
                    text=task_activity_evidence_text(row),
                    version=_value(row, "created_at"),
                    metadata={"task_id": str(_value(row, "task_id")), "user_id": str(_value(row, "user_id")) if _value(row, "user_id") else None},
                )
            )
        comments = await _scalars(
            self.session,
            select(TaskComment).where(TaskComment.task_id.in_(ids)).order_by(TaskComment.created_at.asc()).limit(MAX_EVIDENCE_ITEMS),
        )
        for row in comments:
            self._add(
                _new_item(
                    kind="task_comment",
                    source_id=_value(row, "id"),
                    project_id=self._project_id,
                    relation="task_comment",
                    strength="strong",
                    authorship="user",
                    text=_value(row, "content", ""),
                    version=_value(row, "updated_at") or _value(row, "created_at"),
                    metadata={"task_id": str(_value(row, "task_id")), "user_id": str(_value(row, "user_id"))},
                )
            )
        attachments = await _scalars(
            self.session,
            select(TaskAttachment).where(
                TaskAttachment.task_id.in_(ids), TaskAttachment.project_id == _uuid(self._project_id, field_name="project_id")
            ).limit(MAX_EVIDENCE_ITEMS),
        )
        for row in attachments:
            path = _value(row, "file_path")
            self._add(
                _new_item(
                    kind="task_attachment",
                    source_id=_value(row, "id"),
                    project_id=self._project_id,
                    relation="task_attachment",
                    strength="explicit",
                    authorship="user" if _value(row, "created_by") else "canonical",
                    text=task_attachment_evidence_text(row),
                    version=_value(row, "created_at"),
                    source_path=str(path) if path else None,
                    metadata={"task_id": str(_value(row, "task_id")), "file_path": str(path) if path else None, "size_bytes": _value(row, "size_bytes")},
                )
            )
        references = await _scalars(
            self.session,
            select(TaskReference).where(
                TaskReference.task_id.in_(ids), TaskReference.project_id == _uuid(self._project_id, field_name="project_id")
            ).limit(MAX_EVIDENCE_ITEMS),
        )
        for row in references:
            reference_type = str(_value(row, "reference_type", "")).strip().casefold()
            self._add(
                _new_item(
                    kind="task_reference",
                    source_id=_value(row, "id"),
                    project_id=self._project_id,
                    relation=str(_value(row, "relation_type", "related")),
                    strength="strong" if str(_value(row, "relation_type", "")).casefold() == "source" else "explicit",
                    authorship="user" if _value(row, "created_by") else "canonical",
                    text=_value(row, "display_name", ""),
                    version=_value(row, "created_at"),
                    source_path=_value(row, "target_path") if reference_type == "workspace_file" else None,
                    metadata={
                        "task_id": str(_value(row, "task_id")),
                        "reference_type": reference_type,
                        "target_id": _value(row, "target_id"),
                        "target_path": _value(row, "target_path"),
                        "target_url": _safe_url(_value(row, "target_url")),
                        "metadata": _bounded_json(_value(row, "reference_metadata", {}) or {}),
                    },
                )
            )
        await self._collect_references(references)

    async def _collect_references(self, references: Sequence[Any]) -> None:
        chat_session_ids: set[uuid.UUID] = set()
        chat_message_ids: set[uuid.UUID] = set()
        direct_message_session_ids: dict[uuid.UUID, set[uuid.UUID]] = {}
        node_ids: set[uuid.UUID] = set()
        for row in references:
            reference_type = str(_value(row, "reference_type", "")).casefold()
            target_id = _value(row, "target_id")
            parsed = _maybe_uuid(target_id)
            if reference_type in {"conversation_session", "conversation_message"}:
                reference_session_id, reference_message_id = _chat_reference_ids(row)
                if reference_session_id is not None:
                    chat_session_ids.add(reference_session_id)
                if reference_message_id is not None:
                    chat_message_ids.add(reference_message_id)
                    if reference_session_id is not None:
                        direct_message_session_ids.setdefault(reference_message_id, set()).add(
                            reference_session_id
                        )
            elif reference_type in {"docs_node", "knowledge_node", "document"} and parsed:
                node_ids.add(parsed)
            elif reference_type in {"agent_run", "agent_run_origin", "agent_run_reference"} and parsed:
                run = await _first(
                    self.session,
                    select(AgentRun).where(
                        AgentRun.id == parsed,
                        AgentRun.project_id == _uuid(self._project_id, field_name="project_id"),
                    ),
                )
                if run is not None:
                    self._add(
                        _new_item(
                            kind="agent_run",
                            source_id=_value(run, "id"),
                            project_id=self._project_id,
                            relation="task_reference",
                            strength="explicit",
                            authorship="assistant",
                            text=agent_run_evidence_text(run),
                            version=_value(run, "updated_at") or _value(run, "created_at"),
                            metadata={"task_id": str(_value(run, "task_id")) if _value(run, "task_id") else None, "session_id": str(_value(run, "session_id")) if _value(run, "session_id") else None, "trigger_message_id": str(_value(run, "trigger_message_id")) if _value(run, "trigger_message_id") else None, "status": _value(run, "status")},
                        )
                    )
                    trigger_id = _maybe_uuid(_value(run, "trigger_message_id"))
                    if trigger_id:
                        trigger = await _first(
                            self.session,
                            select(ConversationMessage)
                            .join(ConversationSession, ConversationSession.id == ConversationMessage.session_id)
                            .where(
                                ConversationMessage.id == trigger_id,
                                ConversationMessage.deleted_at.is_(None),
                                ConversationSession.project_id == _uuid(self._project_id, field_name="project_id"),
                                ConversationSession.deleted_at.is_(None),
                            ),
                        )
                        if trigger is not None and not is_privacy_masking_source(trigger):
                            self._add(
                                _new_item(
                                    kind="conversation_message",
                                    source_id=_value(trigger, "id"),
                                    project_id=self._project_id,
                                    relation="agent_run_trigger",
                                    strength="strong" if str(_value(trigger, "role", "")).casefold() == "user" else "supporting",
                                    authorship="user" if str(_value(trigger, "role", "")).casefold() == "user" else "assistant",
                                    text=_value(trigger, "content", ""),
                                    version=_value(trigger, "updated_at") or _value(trigger, "created_at"),
                                    metadata={"session_id": str(_value(trigger, "session_id"))},
                                )
                            )
            elif reference_type == "url":
                safe_url = _safe_url(_value(row, "target_url"))
                if safe_url:
                    self._add(
                        _new_item(
                            kind="url",
                            source_id=safe_url,
                            project_id=self._project_id,
                            relation="task_reference",
                            strength="supporting",
                            authorship="external",
                            text=safe_url,
                            version=_value(row, "created_at"),
                            metadata={"external": True},
                        )
                    )
        if chat_session_ids or chat_message_ids:
            ordered_chat_session_ids = sorted(chat_session_ids, key=str)
            sessions = await _scalars(
                self.session,
                select(ConversationSession).where(
                    ConversationSession.id.in_(ordered_chat_session_ids),
                    ConversationSession.project_id == _uuid(self._project_id, field_name="project_id"),
                    ConversationSession.deleted_at.is_(None),
                ).order_by(ConversationSession.id.asc()).limit(MAX_CHAT_MESSAGES),
            )
            # The API stores a message reference's session in target_id and
            # the message ID in metadata.message_id.  The legacy fallback is
            # retained by _chat_reference_ids for older rows.
            ordered_chat_message_ids = sorted(chat_message_ids, key=str)
            direct_limit = min(
                MAX_EVIDENCE_ITEMS,
                max(MAX_CHAT_MESSAGES, len(ordered_chat_message_ids)),
            )
            direct_messages = await _scalars(
                self.session,
                select(ConversationMessage)
                .join(ConversationSession, ConversationSession.id == ConversationMessage.session_id)
                .where(
                    ConversationMessage.id.in_(ordered_chat_message_ids),
                    ConversationMessage.deleted_at.is_(None),
                    ConversationSession.project_id == _uuid(self._project_id, field_name="project_id"),
                    ConversationSession.deleted_at.is_(None),
                )
                .order_by(ConversationMessage.created_at.asc(), ConversationMessage.id.asc())
                .limit(direct_limit),
            ) if chat_message_ids else []
            direct_messages = [
                row
                for row in direct_messages
                if not direct_message_session_ids.get(_maybe_uuid(_value(row, "id")))
                or str(_value(row, "session_id"))
                in {
                    str(session_id)
                    for session_id in direct_message_session_ids.get(
                        _maybe_uuid(_value(row, "id")), set()
                    )
                }
            ]
            direct_message_evidence_ids = _direct_message_evidence_ids(direct_messages)
            self._mandatory_evidence_ids.update(direct_message_evidence_ids)
            session_ids = {_value(row, "id") for row in sessions}
            if direct_messages:
                extra_session_ids = {
                    _value(row, "session_id")
                    for row in direct_messages
                    if _value(row, "session_id") not in session_ids
                }
                if extra_session_ids:
                    extra_sessions = await _scalars(
                        self.session,
                        select(ConversationSession).where(
                            ConversationSession.id.in_(sorted(extra_session_ids, key=str)),
                            ConversationSession.project_id == _uuid(self._project_id, field_name="project_id"),
                            ConversationSession.deleted_at.is_(None),
                        ).order_by(ConversationSession.id.asc()).limit(MAX_CHAT_MESSAGES),
                    )
                    sessions.extend(extra_sessions)
                    session_ids.update(_value(row, "id") for row in extra_sessions)
            # Project ACL is necessary but a message/session also needs the
            # current actor's ownership or joined participation.
            actor_text = str(_actor_id(self._actor) or "")
            participant_rows = await _scalars(
                self.session,
                select(ConversationParticipant).where(
                    ConversationParticipant.session_id.in_(sorted(session_ids, key=str)),
                    ConversationParticipant.participant_type == "user",
                    ConversationParticipant.participant_id == actor_text,
                    ConversationParticipant.status == "joined",
                ).limit(MAX_CHAT_MESSAGES),
            ) if session_ids and actor_text else []
            participant_session_ids = {
                str(_value(row, "session_id")) for row in participant_rows
            }
            sessions = _ordered_visible_chat_sessions(
                sessions,
                actor_text=actor_text,
                participant_session_ids=participant_session_ids,
            )
            for session in sessions:
                session_id = _value(session, "id")
                self._add(
                    _new_item(
                        kind="conversation_session",
                        source_id=session_id,
                        project_id=self._project_id,
                        relation="task_reference",
                        strength="explicit",
                        authorship="user",
                        text=conversation_session_evidence_text(session),
                        version=_value(session, "last_activity") or _value(session, "session_start"),
                        metadata={"project_id": str(_value(session, "project_id"))},
                    )
                )
            if sessions:
                session_windows: list[tuple[Any, Sequence[Any], Sequence[Any]]] = []
                for session, (head_limit, tail_limit) in zip(
                    sessions, _chat_window_limits(len(sessions))
                ):
                    session_id = _value(session, "id")
                    head_messages = await _scalars(
                        self.session,
                        select(ConversationMessage)
                        .where(
                            ConversationMessage.session_id == session_id,
                            ConversationMessage.deleted_at.is_(None),
                        )
                        .order_by(ConversationMessage.created_at.asc(), ConversationMessage.id.asc())
                        .limit(head_limit),
                    ) if head_limit else []
                    tail_messages = await _scalars(
                        self.session,
                        select(ConversationMessage)
                        .where(
                            ConversationMessage.session_id == session_id,
                            ConversationMessage.deleted_at.is_(None),
                        )
                        .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
                        .limit(tail_limit),
                    ) if tail_limit else []
                    session_windows.append((session_id, head_messages, tail_messages))
                messages = _merge_allocated_chat_windows(session_windows, direct_messages)
                visible_session_ids = {str(_value(row, "id")) for row in sessions}
                for message, mandatory_direct in _deduplicate_chat_messages(
                    messages,
                    mandatory_direct_ids=direct_message_evidence_ids,
                ):
                    if str(_value(message, "session_id")) not in visible_session_ids:
                        continue
                    if is_privacy_masking_source(message):
                        continue
                    role = str(_value(message, "role", "")).casefold()
                    self._add(
                        _new_item(
                            kind="conversation_message",
                            source_id=_value(message, "id"),
                            project_id=self._project_id,
                            relation="task_reference",
                            strength="strong" if role == "user" else "supporting",
                            authorship="user" if role == "user" else "assistant",
                            text=_value(message, "content", ""),
                            version=_value(message, "updated_at") or _value(message, "created_at"),
                            metadata={"session_id": str(_value(message, "session_id")), "role": role},
                        ),
                        mandatory=mandatory_direct,
                    )
        await self._collect_docs_by_ids(node_ids)

    async def _collect_agent_runs(self, task_ids: set[str]) -> None:
        ids = [_maybe_uuid(value) for value in task_ids if _maybe_uuid(value) is not None]
        if not ids:
            return
        runs = await _scalars(
            self.session,
            select(AgentRun).where(
                AgentRun.project_id == _uuid(self._project_id, field_name="project_id"),
                AgentRun.task_id.in_(ids),
            ).order_by(AgentRun.created_at.asc()).limit(48),
        )
        for run in runs:
            run_id = _value(run, "id")
            self._add(
                _new_item(
                    kind="agent_run",
                    source_id=run_id,
                    project_id=self._project_id,
                    relation="agent_run_origin" if _value(run, "task_id") in ids else "agent_run_reference",
                    strength="explicit",
                    authorship="assistant",
                    text=agent_run_evidence_text(run),
                    version=_value(run, "updated_at") or _value(run, "created_at"),
                    metadata={"task_id": str(_value(run, "task_id")) if _value(run, "task_id") else None, "session_id": str(_value(run, "session_id")) if _value(run, "session_id") else None, "trigger_message_id": str(_value(run, "trigger_message_id")) if _value(run, "trigger_message_id") else None, "status": _value(run, "status")},
                )
            )

    async def _collect_docs(self, seed: Any, task_ids: set[str]) -> None:
        node_ids: set[uuid.UUID] = set()
        direct = _maybe_uuid(_value(seed, "knowledge_node_id"))
        if direct:
            node_ids.add(direct)
        node_ids.update(
            _maybe_uuid(item.metadata.get("target_id"))
            for item in self._items.values()
            if item.kind == "task_reference" and str(item.metadata.get("reference_type", "")).casefold() in {"docs_node", "knowledge_node", "document"} and _maybe_uuid(item.metadata.get("target_id"))
        )
        await self._collect_docs_by_ids({item for item in node_ids if item is not None})

    async def _collect_docs_by_ids(self, node_ids: set[uuid.UUID]) -> None:
        if not node_ids:
            return
        nodes = await _scalars(
            self.session,
            select(KnowledgeNode).where(KnowledgeNode.id.in_(list(node_ids)), KnowledgeNode.archived_at.is_(None)).limit(MAX_DOC_REVISIONS),
        )
        authorized: list[Any] = []
        for node in nodes:
            if str(_value(node, "project_id")) == self._project_id:
                authorized.append(node)
                continue
            # A referenced Personal Docs node must remain visible through the
            # current Docs ACL; a stale TaskReference is not permission.
            try:
                predicate = docs_readable_node_predicate(
                    KnowledgeNode,
                    docs_library_id=_value(node, "docs_library_id"),
                    user_id=_actor_id(self._actor),
                    required="read",
                )
                row = await _first(
                    self.session,
                    select(KnowledgeNode).where(KnowledgeNode.id == _value(node, "id"), KnowledgeNode.archived_at.is_(None), predicate),
                )
                if row is not None:
                    authorized.append(row)
            except Exception:
                continue
        for node in authorized:
            self._add(
                _new_item(
                    kind="docs_node",
                    source_id=_value(node, "id"),
                    project_id=self._project_id,
                    relation="task_knowledge_node" if _value(node, "id") == _value(node, "knowledge_node_id") else "task_reference",
                    strength="strong",
                    authorship="user" if _value(node, "created_by") else "canonical",
                    text=docs_node_evidence_text(node),
                    version=_value(node, "updated_at") or _value(node, "created_at"),
                    metadata={"node_type": _value(node, "node_type"), "project_id": str(_value(node, "project_id")) if _value(node, "project_id") else None},
                )
            )
        revisions = await _scalars(
            self.session,
            select(KnowledgeRevision).where(KnowledgeRevision.node_id.in_([_value(node, "id") for node in authorized])).order_by(KnowledgeRevision.created_at.desc()).limit(MAX_DOC_REVISIONS),
        )
        for revision in revisions:
            self._add(
                _new_item(
                    kind="docs_revision",
                    source_id=_value(revision, "id"),
                    project_id=self._project_id,
                    relation="docs_node_revision",
                    strength="strong",
                    authorship="user" if _value(revision, "created_by") else "canonical",
                    text=docs_revision_evidence_text(revision),
                    version=_value(revision, "created_at"),
                    metadata={"node_id": str(_value(revision, "node_id")), "source_refs": _bounded_json(_value(revision, "source_refs_json", []) or [])},
                )
            )

    async def _collect_memories(self, task_ids: set[str]) -> None:
        ids = [_maybe_uuid(value) for value in task_ids if _maybe_uuid(value) is not None]
        rows = await _scalars(
            self.session,
            select(ContextMemory).where(
                ContextMemory.project_id == _uuid(self._project_id, field_name="project_id"),
                ContextMemory.status == "active",
                or_(ContextMemory.scope_type == "project", ContextMemory.task_id.in_(ids) if ids else ContextMemory.scope_type == "project"),
            ).order_by(ContextMemory.importance.desc()).limit(MAX_MEMORY_ROWS),
        )
        for row in rows:
            sensitivity = str(_value(row, "sensitivity", "normal")).casefold()
            if sensitivity in {"secret", "restricted", "critical"}:
                continue
            self._add(
                _new_item(
                    kind="project_memory",
                    source_id=_value(row, "id"),
                    project_id=self._project_id,
                    relation="project_scoped_memory",
                    strength="advisory",
                    authorship="user" if str(_value(row, "created_by_actor", "")).casefold() in {"user", "human"} else "system",
                    text=project_memory_evidence_text(row),
                    version=_value(row, "updated_at") or _value(row, "created_at"),
                    metadata={"memory_type": _value(row, "memory_type"), "task_id": str(_value(row, "task_id")) if _value(row, "task_id") else None, "confidence": _value(row, "confidence"), "trust_level": _value(row, "trust_level")},
                )
            )

    async def _collect_explicit_files(self, seed: Any, task_ids: set[str]) -> None:
        paths: list[str] = []
        for item in self._items.values():
            if item.kind in {"task_attachment", "task_reference"}:
                path = item.source_path or item.metadata.get("target_path") or item.metadata.get("file_path")
                if path:
                    paths.append(str(path))
        project_metadata = _value(self._project, "project_metadata", {}) or _value(_value(seed, "project"), "project_metadata", {}) or {}
        management = project_metadata.get("management", {}) if isinstance(project_metadata, Mapping) else {}
        for key in ("wbs_file", "issue_file", "risk_file"):
            if isinstance(management, Mapping) and management.get(key):
                paths.append(str(management[key]))
        if isinstance(management, Mapping) and isinstance(management.get("request_files"), list):
            paths.extend(str(item) for item in management["request_files"])
        seen: set[str] = set()
        for raw_path in paths:
            try:
                normalized = _relative_workspace_path(raw_path, self._project_id)
                if normalized in seen:
                    continue
                seen.add(normalized)
                file_info = read_bound_project_file(self._project_id, normalized, workspace_root=self.workspace_root)
            except (ValueError, FileNotFoundError, OSError):
                continue
            self._add(
                _new_item(
                    kind="workspace_file",
                    source_id=normalized,
                    project_id=self._project_id,
                    relation="explicit_task_file",
                    strength="explicit",
                    authorship="canonical",
                    text=file_info.get("excerpt", ""),
                    version=workspace_file_version(file_info),
                    source_path=normalized,
                    metadata=file_info,
                )
            )


async def collect_evidence_cluster(
    session: Any,
    seed_task: Any,
    actor: Any,
    **kwargs: Any,
) -> EvidenceCluster:
    return await KnowledgeCaptureEvidenceService(session, **kwargs).collect(seed_task, actor)


async def revalidate_evidence_cluster(
    session: Any,
    cluster: EvidenceCluster,
    actor: Any,
    **kwargs: Any,
) -> EvidenceCluster:
    service = KnowledgeCaptureEvidenceService(session, **kwargs)
    return await service.revalidate(cluster, actor)


EvidenceClusterService = KnowledgeCaptureEvidenceService
KnowledgeCaptureEvidenceCluster = EvidenceCluster


__all__ = [
    "EVIDENCE_AUTHORSHIP",
    "EVIDENCE_KINDS",
    "EVIDENCE_STRENGTHS",
    "EvidenceCluster",
    "EvidenceClusterError",
    "EvidenceClusterService",
    "EvidenceItem",
    "EvidenceScopeError",
    "KnowledgeCaptureEvidenceCluster",
    "KnowledgeCaptureEvidenceService",
    "MAX_GRAPH_DEPTH",
    "collect_evidence_cluster",
    "agent_run_evidence_text",
    "content_hash",
    "conversation_session_evidence_text",
    "docs_node_evidence_text",
    "docs_revision_evidence_text",
    "evidence_text_hash",
    "build_user_confirmation_evidence",
    "project_memory_evidence_text",
    "task_activity_evidence_text",
    "task_attachment_evidence_text",
    "workspace_file_version",
    "read_bound_project_file",
    "revalidate_evidence_cluster",
    "resolve_project_workspace_file",
    "search_bound_project_files",
    "typed_evidence_id",
]
