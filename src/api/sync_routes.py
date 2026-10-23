"""Mobile sync API routes.

Current write support covers Projects, Tasks, time entries, conversation pin/delete
state, and Docs mutations. Remaining tables are pull-only local caches on mobile.
"""

from __future__ import annotations

import base64
import functools
import binascii
import hashlib
import json
import logging
import os
import re
import secrets
import traceback
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import and_, case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..memory.models import (
    ConversationMessage,
    ConversationParticipant,
    ConversationSession,
    AgentDialogue,
    AgentDialogueInput,
    AgentRun,
    ContentDeletionEvent,
    KnowledgeNodeShare,
    Project,
    RecordField,
    RecordRow,
    RecordTable,
    Task,
    TaskAssignee,
    TaskOccurrence,
    TaskRecurrenceRule,
    TaskTag,
    TimeEntry,
    KnowledgeNode,
    DocsLibrary,
    _session_iso,
    public_session_context,
)
from ..memory.project_repository import ProjectRepository
from ..services.app_storage import get_workspaces_root
from ..services.docs_graph_service import DocsGraphService
from ..services.docs_acl import (
    accessible_project_ids as docs_accessible_project_ids,
    docs_readable_node_predicate,
    can_read_node,
    library_can_read,
    library_can_write,
)
from ..services.task_management_service import TaskManagementError, TaskManagementService
from ..services.session_llm_runtime import release_session_agent_team_registry
from ..services.conversation_status import (
    ConversationStatusProjection,
    project_conversation_statuses,
)
from ..services.agent_root_lifecycle import (
    AgentRootLifecycleError,
    attach_private_root_metadata,
    guard_private_root_operation,
    project_private_root_metadata,
)
from ..services.docs_library_compat import read_docs_library_id
from ..services.docs_library_compat import with_legacy_docs_library_aliases
from ..services.docs_workspace import get_project_docs_library
from ..task_time import DEFAULT_TASK_TIMEZONE
from .docs_sync import (
    DOCS_PULL_LIMITS,
    DOCS_SYNC_TABLES,
    DocsOperationError,
    apply_docs_operation,
    docs_scope_digest as calculate_docs_scope_digest,
    docs_scope_revision as calculate_docs_scope_revision,
    docs_composite_response_readable,
    load_current_docs_entity,
    normalize_docs_body_json,
    pull_cursor_envelope,
    pull_docs_table,
    resolve_docs_conflict_library,
)
from .routes.tasks._shared import CreateTaskPayload, _parse_timer_datetime
from .general_sync_digest import (
    GENERAL_DIGEST_VERSION,
    pull_general_digest_page,
    valid_general_digest,
)
from .uuid_http import parse_uuid_or_400
from .datetime_utils import parse_utc_naive_datetime

logger = logging.getLogger(__name__)

_SAFE_SYNC_EXCEPTION_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
_SAFE_SYNC_SQLSTATE_RE = re.compile(r"^[0-9A-Z]{5}$")
_SYNC_REASON_CODE_ALLOWLIST = frozenset(
    {
        "task_cas_stale",
        "task_business_conflict",
        "docs_cas_stale",
        "docs_base_missing",
        "docs_business_conflict",
    }
)
_SYNC_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


def _safe_sync_exception_diagnostics(error: BaseException) -> dict[str, str]:
    """Return bounded diagnostics without exception text, ids, or SQL data."""

    exception_type = type(error).__name__
    if not _SAFE_SYNC_EXCEPTION_TYPE_RE.fullmatch(exception_type):
        exception_type = "Exception"
    sqlstate = ""
    for candidate in (error, getattr(error, "orig", None)):
        value = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if isinstance(value, str) and _SAFE_SYNC_SQLSTATE_RE.fullmatch(value.upper()):
            sqlstate = value.upper()
            break
    source_frame = ""
    for frame in reversed(traceback.extract_tb(error.__traceback__)):
        try:
            relative = os.path.relpath(frame.filename, _SYNC_REPO_ROOT)
        except (TypeError, ValueError):
            continue
        if relative.startswith("src" + os.sep) or relative.startswith("src/"):
            source_frame = f"{os.path.basename(frame.filename)}:{int(frame.lineno)}"
            break
    return {
        "exception_type": exception_type,
        "sqlstate": sqlstate or "unknown",
        "source_frame": source_frame or "unknown",
    }


SYNC_TABLES = {
    "projects",
    "tasks",
    "task_occurrences",
    "time_entries",
    "conversation_sessions",
    "conversation_messages",
    "record_tables",
    "record_fields",
    "record_rows",
    *DOCS_SYNC_TABLES,
}

# push が Docs ハンドラ（apply_docs_operation）へ委譲するテーブル。
DOCS_PUSH_TABLES = {
    "knowledge_nodes",
    "knowledge_supertags",
    "knowledge_node_supertags",
    "knowledge_field_values",
}

SYNC_PULL_LIMITS = {
    "tasks": 1000,
    "task_occurrences": 1000,
    "time_entries": 1000,
    "conversation_sessions": 5000,
    "conversation_messages": 5000,
    "record_tables": 1000,
    "record_fields": 5000,
    "record_rows": 5000,
    **DOCS_PULL_LIMITS,
}

# General (non-Docs) pulls used to return every changed entity in one response.
# Conversation message metadata can be several megabytes per row, so the
# legacy 5,000-row cap still produces an unsafe response for mobile's JSON
# parser.  The additive protocol below keeps legacy clients unchanged while
# allowing newer clients to consume a bounded keyset page at a time.
GENERAL_SYNC_PAGINATION_VERSION = 1
DOCS_SYNC_PROTOCOL_VERSION = 2
GENERAL_SYNC_PAGE_LIMITS = {
    "projects": 100,
    "tasks": 100,
    "task_occurrences": 100,
    "time_entries": 100,
    "conversation_sessions": 100,
    # A conversation message may contain a large model transcript/tool result;
    # keep this page deliberately smaller than the other general tables.
    # Live device data contains multi-megabyte metadata rows; 16 keeps the
    # UTF-16/JSON parse peak below the Android Java-heap growth limit even when
    # a page happens to cluster the largest transcripts.
    "conversation_messages": 16,
    "record_tables": 100,
    "record_fields": 100,
    "record_rows": 100,
}


class SyncOperation(BaseModel):
    op_id: str
    table: str
    action: str
    entity_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    base_updated_at: Optional[str] = None


class SyncPushPayload(BaseModel):
    operations: list[SyncOperation] = Field(default_factory=list)


class SyncPullPayload(BaseModel):
    """pull 要求の任意ボディ。

    ``docs_digests`` は「テーブル名 → 前回サーバが返したダイジェスト文字列」のエコー。
    Docs の削除伝播テーブルで、サーバの現在ダイジェストと一致すれば権威セット全量
    の返却を省く（定常時の応答をほぼ空にする）。未指定・未知テーブルは無視する。
    """

    docs_digests: dict[str, str] = Field(default_factory=dict)
    docs_cursors: dict[str, str] = Field(default_factory=dict)
    # Optional Docs composite discriminator.  ``project_id`` is meaningful
    # together with ``docs_scope_id`` on the route; non-Docs pull tables keep
    # their historical all-accessible-project behavior.
    project_id: Optional[str] = None
    docs_scope_id: Optional[str] = None
    # Additive Docs pagination metadata.  These are optional so non-Docs sync
    # and legacy v2 mobile clients retain their existing request contract.
    docs_snapshot_token: Optional[str] = None
    docs_scope_revision: Optional[str] = None
    # Additive general-table keyset pagination.  Kept separate from the Docs
    # cursor envelope so each protocol can evolve independently.
    general_cursors: dict[str, str] = Field(default_factory=dict)
    general_snapshot_token: Optional[str] = None
    general_digests: dict[str, str] = Field(default_factory=dict)


class SyncDocsDiagnosticResponse(BaseModel):
    """Bounded metadata returned by the read-only Docs sync probe."""

    protocol_version: Literal[1] = 1
    docs_sync_protocol_version: Literal[2] = DOCS_SYNC_PROTOCOL_VERSION
    read_only: Literal[True] = True
    server_phase: Literal["metadata_read"] = "metadata_read"
    status: Literal["ok", "error"]
    personal_library_exists: bool | None = None
    needs_normalization: bool | None = Field(
        default=None,
        description=(
            "True when the current user owns a Docs Library row whose type is "
            "not personal; this diagnostic never normalizes it."
        ),
    )
    accessible_project_count: int | None = Field(default=None, ge=0)
    accessible_scope_count: int | None = Field(default=None, ge=0)
    error_code: Literal["diagnostic_query_failed"] | None = None
    exception_type: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$",
    )


async def _read_pull_docs_digests(request: Request) -> dict[str, str]:
    """GET /pull の docs_digests を取り出す（無ければ空 dict）。

    モバイルの fetch は GET にボディを付けられないため、クエリパラメータ
    ``docs_digests``（URLエンコードした JSON object）を優先して読む。
    後方の任意ボディ読み取りはテスト・ツール向けのフォールバック。
    """
    from_query = request.query_params.get("docs_digests")
    if from_query:
        try:
            return SyncPullPayload.model_validate({"docs_digests": json.loads(from_query)}).docs_digests
        except Exception:  # noqa: BLE001
            return {}
    try:
        raw = await request.body()
    except Exception:  # noqa: BLE001
        return {}
    if not raw:
        return {}
    try:
        return SyncPullPayload.model_validate_json(raw).docs_digests
    except Exception:  # noqa: BLE001
        # 不正なボディでも pull 全体は落とさず、従来どおり全量 pull にフォールバックする。
        return {}


async def _read_pull_docs_cursors(request: Request) -> dict[str, str]:
    """Docsテーブルごとの不透明な次ページcursorを取り出す。"""
    from_query = request.query_params.get("docs_cursors")
    if from_query:
        try:
            return SyncPullPayload.model_validate(
                {"docs_cursors": json.loads(from_query)}
            ).docs_cursors
        except Exception:  # noqa: BLE001
            return {}
    return {}


async def _read_pull_docs_snapshot_token(request: Request) -> Optional[str]:
    """Read the opaque Docs snapshot token from query or optional GET body."""

    from_query = request.query_params.get("docs_snapshot_token")
    if from_query:
        return from_query
    try:
        raw = await request.body()
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        return SyncPullPayload.model_validate_json(raw).docs_snapshot_token
    except Exception:  # noqa: BLE001
        return None


async def _read_pull_docs_scope_revision(request: Request) -> Optional[str]:
    """Read the opaque ACL revision from query or optional GET body."""

    from_query = request.query_params.get("docs_scope_revision")
    if from_query:
        return from_query
    try:
        raw = await request.body()
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        return SyncPullPayload.model_validate_json(raw).docs_scope_revision
    except Exception:  # noqa: BLE001
        return None


async def _read_pull_project_id(request: Request) -> Optional[str]:
    """Read an optional Docs project discriminator from query or GET body."""

    from_query = request.query_params.get("project_id")
    if from_query:
        return from_query
    try:
        raw = await request.body()
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        return SyncPullPayload.model_validate_json(raw).project_id
    except Exception:  # noqa: BLE001
        return None


async def _read_pull_docs_scope_id(request: Request) -> Optional[str]:
    """Read the Docs library scope from query or optional GET body."""

    from_query = request.query_params.get("docs_scope_id")
    if from_query:
        return from_query
    try:
        raw = await request.body()
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        return SyncPullPayload.model_validate_json(raw).docs_scope_id
    except Exception:  # noqa: BLE001
        return None


async def _read_pull_general_cursors(request: Request) -> dict[str, str]:
    """Read general-table cursors from the query string (JSON object)."""

    from_query = request.query_params.get("general_cursors")
    if from_query:
        try:
            return SyncPullPayload.model_validate(
                {"general_cursors": json.loads(from_query)}
            ).general_cursors
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=400, detail="Invalid general sync cursors"
            ) from exc
    try:
        raw = await request.body()
    except Exception:  # noqa: BLE001
        return {}
    if not raw:
        return {}
    try:
        return SyncPullPayload.model_validate_json(raw).general_cursors
    except Exception:  # noqa: BLE001
        # Preserve the legacy GET/body fallback semantics.  A malformed or
        # unrelated body must not break an otherwise valid pull; query
        # parameters remain strict because they explicitly opt into v1.
        return {}


async def _read_pull_general_snapshot_token(request: Request) -> Optional[str]:
    """Read the opaque general snapshot token from the query string."""

    from_query = request.query_params.get("general_snapshot_token")
    if from_query:
        return from_query
    try:
        raw = await request.body()
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        return SyncPullPayload.model_validate_json(raw).general_snapshot_token
    except Exception:  # noqa: BLE001
        return None


async def _read_pull_general_digests(request: Request) -> dict[str, str]:
    """Read completed content acknowledgements; they never become ``since``."""
    from_query = request.query_params.get("general_digests")
    try:
        if from_query is not None:
            digests = SyncPullPayload.model_validate(
                {"general_digests": json.loads(from_query)}
            ).general_digests
        else:
            raw = await request.body()
            digests = SyncPullPayload.model_validate_json(raw).general_digests if raw else {}
    except Exception as exc:  # noqa: BLE001
        if from_query is None:
            return {}
        raise HTTPException(status_code=400, detail="Invalid general sync digests") from exc
    if any(table not in GENERAL_SYNC_PAGE_LIMITS or not valid_general_digest(value)
           for table, value in digests.items()):
        raise HTTPException(status_code=400, detail="Invalid general sync digests")
    return digests


def _validate_docs_opaque_token(value: Optional[str], field_name: str) -> Optional[str]:
    """Validate token shape without interpreting its opaque contents."""

    if value in (None, ""):
        return None
    # Cursor envelopes are untrusted JSON.  Do not coerce numbers/arrays into
    # strings: the Director contract requires a non-empty opaque *string* and
    # a coerced value could accidentally make malformed metadata look valid.
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}")
    token = value.strip()
    # URL-safe random tokens are intentionally opaque.  Length limits prevent
    # an accidental unbounded query value while accepting future token formats.
    if not token or len(token) > 512 or any(ord(char) < 0x20 for char in token):
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}")
    return token


def _parse_datetime(value: Optional[str], field_name: str) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        return parse_utc_naive_datetime(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}") from exc


def _parse_wall_clock_datetime(
    value: Optional[str], field_name: str
) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1]
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            parsed = parsed.replace(tzinfo=None)
        return parsed
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}") from exc


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _ensure_not_stale(
    current_updated_at: Optional[datetime],
    base_updated_at: Optional[str],
    *,
    reason_code: str | None = None,
) -> None:
    if current_updated_at is None or base_updated_at in (None, ""):
        return
    base_dt = _parse_datetime(base_updated_at, "base_updated_at")
    if base_dt is not None and current_updated_at > base_dt:
        detail = {"code": reason_code} if reason_code else None
        raise TaskManagementError(
            "Entity was updated on the server",
            status_code=409,
            detail=detail,
        )


async def _accessible_project_ids(session: AsyncSession, user_id: UUID) -> list[UUID]:
    # Use the shared Project ACL policy rather than raw membership rows.  This
    # includes project owners and global admins (who need not have a
    # ProjectMember row), while excluding disabled/read=false memberships.
    return await docs_accessible_project_ids(session, user_id)


async def _docs_sync_scopes(
    session: AsyncSession,
    *,
    user_id: UUID,
    project_ids: list[UUID],
    personal_library: DocsLibrary | None = None,
    personal_workspace: DocsLibrary | None = None,
) -> list[dict[str, Any]]:
    """Return the Docs workspaces visible to mobile sync.

    The legacy endpoint only pulled the actor's personal library.  Docs ACL
    now permits explicit Personal subtree shares and canonical Project
    workspaces, so advertise every currently readable scope and let the client
    pull each scope independently (with a scope-specific cursor/digest).
    """

    # ``personal_workspace`` is accepted only as an in-process compatibility
    # alias; the canonical service/API name is ``personal_library``.
    personal_library = personal_library or personal_workspace
    libraries: dict[UUID, DocsLibrary] = (
        {personal_library.id: personal_library}
        if personal_library is not None
        else {}
    )
    project_scopes: list[tuple[UUID, DocsLibrary, str]] = []
    if project_ids:
        # Project Docs now live in the owner's Personal Library.  A Project
        # member must still receive a project-scoped entry even though
        # ``library_can_read`` is (correctly) owner-only for Personal
        # libraries.  Resolve through the SELECT-only pointer validator so a
        # stale/malformed project pointer is never advertised.
        for project_id in sorted(set(project_ids), key=str):
            try:
                library = await get_project_docs_library(
                    session,
                    project_id=project_id,
                    actor_user_id=user_id,
                )
            except (PermissionError, ValueError):
                library = None
            if library is None:
                continue
            can_write = await ProjectRepository.has_permission(
                session,
                project_id=project_id,
                user_id=user_id,
                permission="write",
            )
            libraries[library.id] = library
            project_scopes.append(
                (project_id, library, "write" if can_write else "read")
            )

    shared_query = (
        select(DocsLibrary, KnowledgeNodeShare.permission)
        .join(KnowledgeNode, KnowledgeNode.docs_library_id == DocsLibrary.id)
        .join(KnowledgeNodeShare, KnowledgeNodeShare.node_id == KnowledgeNode.id)
        .where(
            KnowledgeNodeShare.user_id == user_id,
            DocsLibrary.library_type == "personal",
            KnowledgeNode.archived_at.is_(None),
        )
    )
    if personal_library is not None:
        shared_query = shared_query.where(DocsLibrary.id != personal_library.id)
    shared_rows = await session.execute(shared_query)
    shared_permissions: dict[UUID, str] = {}
    for library, permission in shared_rows.all():
        libraries[library.id] = library
        value = str(permission or "read").lower()
        if shared_permissions.get(library.id) != "write":
            shared_permissions[library.id] = "write" if value == "write" else "read"

    scopes: list[dict[str, Any]] = []
    if personal_library is not None:
        scopes.append(
            with_legacy_docs_library_aliases(
                {
                    "docs_library_id": str(personal_library.id),
                    "source": "personal",
                    "access": "owner",
                    "read_only": False,
                },
                personal_library.id,
            )
        )
    # Keep one explicit scope per readable Project.  Multiple projects may
    # share the same owner's Personal Library; the project_id discriminator
    # prevents clients from treating the whole Personal Library as shared.
    for project_id, library, access in sorted(project_scopes, key=lambda item: str(item[0])):
        scopes.append(
            with_legacy_docs_library_aliases(
                {
                    "docs_library_id": str(library.id),
                    "project_id": str(project_id),
                    "source": "project",
                    "access": access,
                    "read_only": access == "read",
                },
                library.id,
            )
        )

    project_library_ids = {library.id for _, library, _ in project_scopes}
    for library in sorted(libraries.values(), key=lambda item: str(item.id)):
        if (
            (personal_library is not None and library.id == personal_library.id)
            or library.id in project_library_ids
        ):
            continue
        # Personal subtree shares are advertised only as a shared scope.  The
        # pull endpoint still applies node-level ACL filtering, so metadata
        # from unrelated private nodes cannot leak through this announcement.
        scopes.append(
            with_legacy_docs_library_aliases(
                {
                    "docs_library_id": str(library.id),
                    "source": "shared",
                    "access": shared_permissions.get(library.id, "read"),
                    "read_only": shared_permissions.get(library.id, "read") == "read",
                },
                library.id,
            )
        )
    return scopes


def _split_changes(rows: list[Any]) -> dict[str, Any]:
    changes = []
    tombstones = []
    for row in rows:
        deleted_at = getattr(row, "deleted_at", None)
        if deleted_at is None:
            changes.append(row.to_dict())
        else:
            tombstone = {"id": str(row.id), "deleted_at": _iso(deleted_at)}
            deletion_batch_id = getattr(row, "deletion_batch_id", None)
            if deletion_batch_id:
                tombstone["deletion_batch_id"] = str(deletion_batch_id)
            tombstones.append(tombstone)
    return {"changes": changes, "tombstones": tombstones, "cursor": None}


def _general_scope_fingerprint(user_id: UUID, project_ids: list[UUID]) -> str:
    """Return a deterministic ACL scope binding for one general pull run."""

    material = "|".join(
        [str(user_id), *(str(project_id) for project_id in sorted(project_ids, key=str))]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _GeneralSnapshotDetails:
    snapshot_at: datetime
    scope: Optional[str]
    watermark: Optional[datetime]
    stable: bool


def _encode_general_snapshot_token(
    server_time: datetime,
    scope_fingerprint: Optional[str] = None,
    *,
    watermark: Optional[datetime] = None,
    stable: bool = False,
) -> str:
    """Encode the immutable boundary and replay watermark for one run.

    The token is intentionally opaque to clients.  It carries no authority by
    itself; it pins the query upper bound so rows created/updated while a
    multi-page pull is in flight are picked up by the next run instead of
    moving between pages.  ``stable=False`` means this protocol has not
    established a commit-ordered boundary.  General v1 intentionally sets it
    to false for every run because application timestamps are assigned before
    the first database write; the client must therefore replay from
    ``since=null`` after every bounded traversal.
    """

    payload = {
        "v": GENERAL_SYNC_PAGINATION_VERSION,
        "server_time": server_time.isoformat(),
        "nonce": secrets.token_urlsafe(12),
        "stable": bool(stable),
        "watermark": watermark.isoformat() if watermark is not None else None,
    }
    if scope_fingerprint:
        payload["scope"] = scope_fingerprint
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return encoded.rstrip("=")


def _decode_general_snapshot_details(
    value: Optional[str],
) -> Optional[_GeneralSnapshotDetails]:
    if not value:
        return None
    try:
        padding = "=" * (-len(value) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode((value + padding).encode("ascii")).decode(
                "utf-8"
            )
        )
    except (
        ValueError,
        TypeError,
        UnicodeEncodeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        raise ValueError("Invalid general sync snapshot token") from exc
    if not isinstance(payload, dict) or payload.get("v") != GENERAL_SYNC_PAGINATION_VERSION:
        raise ValueError("Invalid general sync snapshot token")
    server_time = payload.get("server_time")
    if not isinstance(server_time, str) or not payload.get("nonce"):
        raise ValueError("Invalid general sync snapshot token")
    parsed = _parse_datetime(server_time, "general_snapshot_token")
    if parsed is None:
        raise ValueError("Invalid general sync snapshot token")
    # A client must not be able to pin a run arbitrarily far in the future.
    # Small clock skew is harmless; a future boundary would otherwise turn a
    # continuation into an unbounded full pull.
    if parsed > datetime.utcnow():
        raise ValueError("Invalid general sync snapshot token")
    scope = payload.get("scope")
    if scope is not None and (
        not isinstance(scope, str)
        or len(scope) != hashlib.sha256().digest_size * 2
        or any(character not in "0123456789abcdef" for character in scope.lower())
    ):
        raise ValueError("Invalid general sync snapshot token")
    # Tokens issued by the first v1 build had no stability metadata.  They are
    # intentionally treated as unsafe and therefore force replay at
    # completion instead of trusting a potentially truncated watermark.
    stable = payload.get("stable") is True
    raw_watermark = payload.get("watermark")
    if raw_watermark is None:
        watermark = None
    elif isinstance(raw_watermark, str):
        watermark = _parse_datetime(raw_watermark, "general_snapshot_watermark")
        if watermark is None or watermark > parsed:
            raise ValueError("Invalid general sync snapshot token")
    else:
        raise ValueError("Invalid general sync snapshot token")
    if not stable:
        watermark = None
    return _GeneralSnapshotDetails(parsed, scope, watermark, stable)


def _decode_general_snapshot_metadata(
    value: Optional[str],
) -> Optional[tuple[datetime, Optional[str]]]:
    details = _decode_general_snapshot_details(value)
    return (details.snapshot_at, details.scope) if details is not None else None


def _decode_general_snapshot_token(value: Optional[str]) -> Optional[datetime]:
    metadata = _decode_general_snapshot_metadata(value)
    return metadata[0] if metadata is not None else None


def _encode_general_cursor(
    table: str, event_at: datetime, entity_id: Any
) -> str:
    payload = {
        "v": GENERAL_SYNC_PAGINATION_VERSION,
        "table": table,
        "event_at": event_at.isoformat(),
        "id": str(entity_id),
    }
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")


def _decode_general_cursor(
    value: Optional[str], table: str
) -> Optional[tuple[datetime, UUID]]:
    if not value:
        return None
    try:
        padding = "=" * (-len(value) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode((value + padding).encode("ascii")).decode(
                "utf-8"
            )
        )
    except (
        ValueError,
        TypeError,
        UnicodeEncodeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        raise ValueError("Invalid general sync cursor") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("v") != GENERAL_SYNC_PAGINATION_VERSION
        or payload.get("table") != table
        or not isinstance(payload.get("event_at"), str)
        or not isinstance(payload.get("id"), str)
    ):
        raise ValueError("Invalid general sync cursor")
    event_at = _parse_datetime(payload["event_at"], "general_cursor")
    if event_at is None:
        raise ValueError("Invalid general sync cursor")
    try:
        entity_id = UUID(payload["id"])
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid general sync cursor") from exc
    return event_at, entity_id


def _general_event_expression(model: Any) -> Any:
    """Return the timestamp used for stable general-pull ordering."""

    updated_at = getattr(model, "updated_at", None)
    created_at = getattr(model, "created_at", None)
    deleted_at = getattr(model, "deleted_at", None)
    if updated_at is None and created_at is None and deleted_at is None:
        raise ValueError("General sync model has no timestamp column")
    # ``updated_at`` is the authoritative mutation clock used by the legacy
    # pull path.  A small legacy slice has NULL updated_at, so fall back to
    # created_at for those rows only.  Tombstones still win over the base row.
    # Keeping this COALESCE policy in lockstep with _row_general_event_at is
    # important: some historical rows have a local-time created_at that is
    # newer than their UTC updated_at, and treating created_at as a second
    # mutation clock would make the emitted cursor differ from SQL ordering.
    if updated_at is not None and created_at is not None:
        base_event_at = func.coalesce(updated_at, created_at)
    else:
        base_event_at = updated_at if updated_at is not None else created_at
    # A handful of imported legacy messages have neither timestamp.  Give
    # them a deterministic lower-bound key instead of letting SQL NULL
    # ordering silently drop them from a full pull.  _general_page_statement
    # explicitly keeps this null-timestamp slice visible even when since is
    # already advanced; such rows are rare and are safely idempotent locally.
    if base_event_at is not None:
        base_event_at = func.coalesce(base_event_at, literal(datetime.min))
    else:
        base_event_at = literal(datetime.min)
    if deleted_at is None:
        return base_event_at
    return func.greatest(base_event_at, deleted_at)


def _general_page_statement(
    stmt: Any,
    model: Any,
    *,
    table: str,
    since: Optional[datetime],
    snapshot_at: datetime,
    cursor: Optional[str],
    limit: int,
    event_expression: Any | None = None,
) -> Any:
    event_at = (
        event_expression
        if event_expression is not None
        else _general_event_expression(model)
    )
    stmt = stmt.where(event_at <= snapshot_at)
    if since:
        since_filter = event_at > since
        updated_column = getattr(model, "updated_at", None)
        created_column = getattr(model, "created_at", None)
        deleted_column = getattr(model, "deleted_at", None)
        null_timestamp_columns = [
            column
            for column in (updated_column, created_column, deleted_column)
            if column is not None
        ]
        if null_timestamp_columns:
            # Keep rows whose entire timestamp tuple is NULL visible on every
            # run.  Their deterministic datetime.min key prevents cursor
            # livelock; the mobile upsert remains idempotent until a later
            # server mutation supplies a real timestamp.
            null_timestamp = and_(*[column.is_(None) for column in null_timestamp_columns])
            stmt = stmt.where(or_(since_filter, null_timestamp))
        else:
            stmt = stmt.where(since_filter)
    decoded = _decode_general_cursor(cursor, table)
    if decoded:
        cursor_time, cursor_id = decoded
        stmt = stmt.where(
            or_(
                event_at < cursor_time,
                and_(event_at == cursor_time, model.id < cursor_id),
            )
        )
    return stmt.order_by(event_at.desc(), model.id.desc()).limit(limit + 1)


def _row_event_at(row: Any, *fields: str) -> Optional[datetime]:
    values = [getattr(row, field, None) for field in fields]
    values = [value for value in values if value is not None]
    return max(values) if values else None


def _row_general_event_at(row: Any) -> Optional[datetime]:
    """Match _general_event_expression's updated/created/deleted ordering."""

    base = getattr(row, "updated_at", None) or getattr(row, "created_at", None)
    deleted_at = getattr(row, "deleted_at", None)
    values = [value for value in (base, deleted_at) if value is not None]
    return max(values) if values else datetime.min


def _conversation_row_event_at(row: Any) -> Optional[datetime]:
    """Read the SQL dependency clock retained on conversation ORM rows."""

    event_at = _normalize_sync_datetime(
        getattr(row, _CONVERSATION_EVENT_ATTR, None)
    )
    if event_at is not None:
        return event_at
    values = [
        _normalize_sync_datetime(getattr(row, field, None))
        for field in (
            "last_activity",
            "last_read_at",
            "pin_updated_at",
            "agent_binding_updated_at",
            "session_start",
            "deleted_at",
        )
    ]
    values = [value for value in values if value is not None]
    return max(values) if values else None


def _general_page_payload(
    rows: list[Any],
    *,
    table: str,
    limit: int,
    splitter,
    event_getter,
) -> dict[str, Any]:
    page = rows[:limit]
    payload = splitter(page)
    next_cursor = None
    if len(rows) > limit and page:
        event_at = event_getter(page[-1])
        if event_at is not None:
            next_cursor = _encode_general_cursor(table, event_at, page[-1].id)
    payload["cursor"] = next_cursor
    return payload


_CONVERSATION_EVENT_ATTR = "_conversation_sync_event_at"


def _history_completion_iso(value: Any) -> Optional[str]:
    """Serialize status fences as explicit UTC for the sync wire contract."""

    if value is None:
        return None
    if isinstance(value, datetime):
        normalized = (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )
        return normalized.isoformat().replace("+00:00", "Z")
    return str(value)


def _normalize_sync_datetime(value: Any) -> datetime | None:
    """Normalize ORM/SQLite timestamp values before comparing them."""

    if value is None:
        return None
    if isinstance(value, datetime):
        return (
            value.astimezone(timezone.utc).replace(tzinfo=None)
            if value.tzinfo is not None
            else value
        )
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (
        parsed.astimezone(timezone.utc).replace(tzinfo=None)
        if parsed.tzinfo is not None
        else parsed
    )


def _apply_conversation_status_projection(
    payload: dict[str, Any],
    projection: ConversationStatusProjection | None,
) -> dict[str, Any]:
    """Apply the shared status fields to an already serialized session."""

    if projection is None:
        return payload
    status = str(projection.status)
    payload["history_status"] = status
    payload["history_completed_at"] = _history_completion_iso(
        projection.history_completed_at
    )
    payload["is_unread"] = status == "ready"
    return payload


def _conversation_session_payload(
    row: ConversationSession,
    *,
    status_projection: ConversationStatusProjection | None = None,
) -> dict[str, Any]:
    payload = project_private_root_metadata(
        row.to_dict(),
        getattr(row, "_private_agent_root_metadata", None),
    )
    payload["history_status"] = "idle"
    payload["history_completed_at"] = None
    payload["is_unread"] = False
    timestamps = [
        normalized
        for value in (
            row.last_activity,
            row.last_read_at,
            row.pin_updated_at,
            row.agent_binding_updated_at,
            row.session_start,
            getattr(row, _CONVERSATION_EVENT_ATTR, None),
        )
        for normalized in (_normalize_sync_datetime(value),)
        if normalized is not None
    ]
    payload["updated_at"] = _session_iso(max(timestamps) if timestamps else None)
    payload["created_at"] = _session_iso(row.session_start)
    payload["session_metadata"] = public_session_context(row.context) or {}
    return _apply_conversation_status_projection(payload, status_projection)


def _record_table_payload(row: RecordTable) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "name": row.name,
        "description": row.description,
        "icon": row.icon,
        "sort_order": row.sort_order,
        "schema_version": row.schema_version,
        "memory_policy": row.memory_policy,
        "default_sensitivity": row.default_sensitivity,
        "metadata": row.table_metadata or {},
        "created_by": str(row.created_by) if row.created_by else None,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "deleted_at": _iso(row.deleted_at),
    }


def _record_field_payload(row: RecordField) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "table_id": str(row.table_id),
        "key": row.key,
        "label": row.label,
        "field_type": row.field_type,
        "options": row.options or {},
        "required": bool(row.required),
        "unique_value": bool(row.unique_value),
        "sort_order": row.sort_order,
        "is_title": bool(row.is_title),
        "is_due": bool(row.is_due),
        "sensitivity": row.sensitivity,
        "metadata": row.field_metadata or {},
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "deleted_at": _iso(row.deleted_at),
    }


def _record_row_payload(row: RecordRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "table_id": str(row.table_id),
        "project_id": str(row.project_id),
        "created_by": str(row.created_by) if row.created_by else None,
        "values": row.values or {},
        "title": row.title,
        "status": row.status,
        "due_at": _iso(row.due_at),
        "search_text": row.search_text,
        "sensitivity": row.sensitivity,
        "metadata": row.row_metadata or {},
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "deleted_at": _iso(row.deleted_at),
    }


def _split_record_changes(rows: list[Any], serializer) -> dict[str, Any]:
    changes = []
    tombstones = []
    for row in rows:
        if getattr(row, "deleted_at", None) is None:
            changes.append(serializer(row))
        else:
            tombstones.append({"id": str(row.id), "deleted_at": _iso(row.deleted_at)})
    return {"changes": changes, "tombstones": tombstones, "cursor": None}


def _conversation_visibility_filter(
    user_id: UUID, project_ids: list[UUID]
) -> Any:
    """Match the conversation API's owner/participant and Project read ACLs.

    Legacy ``default_user`` rows are not public. They require an explicit
    joined participant just like any other user's conversation; never use
    the legacy owner name as an authenticated user's identity alias.
    """
    actor_id = str(user_id)
    participant_session_ids = select(ConversationParticipant.session_id).where(
        ConversationParticipant.participant_type == "user",
        ConversationParticipant.participant_id == actor_id,
        ConversationParticipant.status == "joined",
    )
    return and_(
        or_(
            ConversationSession.user_id == actor_id,
            ConversationSession.id.in_(participant_session_ids),
        ),
        or_(
            ConversationSession.project_id.is_(None),
            ConversationSession.project_id.in_(project_ids),
        ),
    )


async def _pull_projects(
    session: AsyncSession,
    project_ids: list[UUID],
    since: Optional[datetime],
    *,
    general_pagination: bool = False,
    general_cursor: Optional[str] = None,
    general_snapshot_at: Optional[datetime] = None,
) -> dict[str, Any]:
    if not project_ids:
        return _split_changes([])
    stmt = select(Project).where(Project.id.in_(project_ids))
    if general_pagination:
        if general_snapshot_at is None:
            raise ValueError("General sync snapshot is missing")
        stmt = _general_page_statement(
            stmt,
            Project,
            table="projects",
            since=since,
            snapshot_at=general_snapshot_at,
            cursor=general_cursor,
            limit=GENERAL_SYNC_PAGE_LIMITS["projects"],
        )
    elif since:
        stmt = stmt.where(
            or_(Project.updated_at > since, Project.deleted_at > since)
        )
    result = await session.execute(stmt)
    rows = list(result.scalars().all())
    if general_pagination:
        return _general_page_payload(
            rows,
            table="projects",
            limit=GENERAL_SYNC_PAGE_LIMITS["projects"],
            splitter=_split_changes,
            event_getter=_row_general_event_at,
        )
    return _split_changes(rows)


async def _pull_tasks(
    session: AsyncSession,
    project_ids: list[UUID],
    since: Optional[datetime],
    *,
    general_pagination: bool = False,
    general_cursor: Optional[str] = None,
    general_snapshot_at: Optional[datetime] = None,
) -> dict[str, Any]:
    if not project_ids:
        return _split_changes([])
    stmt = (
        select(Task)
        .options(
            selectinload(Task.project),
            selectinload(Task.assignees).selectinload(TaskAssignee.user),
            selectinload(Task.recurrence_rule),
            selectinload(Task.task_tags).selectinload(TaskTag.tag),
        )
        .where(Task.project_id.in_(project_ids))
    )
    if general_pagination:
        if general_snapshot_at is None:
            raise ValueError("General sync snapshot is missing")
        stmt = _general_page_statement(
            stmt,
            Task,
            table="tasks",
            since=since,
            snapshot_at=general_snapshot_at,
            cursor=general_cursor,
            limit=GENERAL_SYNC_PAGE_LIMITS["tasks"],
        )
    else:
        if since:
            stmt = stmt.where(or_(Task.updated_at > since, Task.deleted_at > since))
        stmt = stmt.order_by(Task.updated_at.desc()).limit(SYNC_PULL_LIMITS["tasks"])
    result = await session.execute(stmt)
    rows = list(result.scalars().unique().all())
    if not general_pagination:
        payload = _split_changes(rows)
    else:
        if general_snapshot_at is None:
            raise ValueError("General sync snapshot is missing")

        # A purged task has no row left to return from the ordinary Task
        # query.  Merge the bounded deletion-ledger stream with the bounded
        # live-row stream before choosing the page boundary.  Both streams use
        # the same (event_at, UUID) descending key, so a cursor advances past
        # *all* task lifecycle events instead of silently ending after the
        # first 100 live rows.  The two streams are intentionally kept as
        # independent records: mobile tombstone application is idempotent and
        # compares server timestamps, while retaining both records avoids
        # needing to materialize the unbounded ledger to de-duplicate it.
        task_limit = GENERAL_SYNC_PAGE_LIMITS["tasks"]
        ledger_stmt = select(ContentDeletionEvent).where(
            ContentDeletionEvent.entity_type == "task",
            ContentDeletionEvent.project_id.in_(project_ids),
            ContentDeletionEvent.action.in_(
                ("deleted", "purged", "permanent_deleted")
            ),
            ContentDeletionEvent.event_at <= general_snapshot_at,
        )
        if since:
            ledger_stmt = ledger_stmt.where(ContentDeletionEvent.event_at > since)
        decoded_cursor = _decode_general_cursor(general_cursor, "tasks")
        if decoded_cursor:
            cursor_time, cursor_id = decoded_cursor
            ledger_stmt = ledger_stmt.where(
                or_(
                    ContentDeletionEvent.event_at < cursor_time,
                    and_(
                        ContentDeletionEvent.event_at == cursor_time,
                        ContentDeletionEvent.id < cursor_id,
                    ),
                )
            )
        ledger_stmt = ledger_stmt.order_by(
            ContentDeletionEvent.event_at.desc(),
            ContentDeletionEvent.id.desc(),
        ).limit(task_limit + 1)
        ledger_result = await session.execute(ledger_stmt)
        ledger_events = list(ledger_result.scalars().all())

        stream: list[tuple[datetime, UUID, str, Any]] = []
        for row in rows:
            event_at = _row_general_event_at(row)
            if event_at is not None:
                stream.append((event_at, UUID(str(row.id)), "row", row))
        for event in ledger_events:
            if event.event_at is not None:
                stream.append((event.event_at, UUID(str(event.id)), "ledger", event))
        stream.sort(key=lambda item: (item[0], item[1]), reverse=True)

        page = stream[:task_limit]
        changes = []
        tombstones = []
        # If a retained Task row and its deletion-ledger event share the same
        # timestamp, prefer the row as the canonical current state.  This
        # avoids a UUID tie-break putting a stale tombstone ahead of an active
        # restored row; timestamp-based mobile LWW then converges correctly for
        # any pair that lands in this bounded page.
        winners: dict[str, tuple[datetime, UUID, str, Any]] = {}
        for record in page:
            entity_id = (
                str(record[3].id)
                if record[2] == "row"
                else str(record[3].entity_id)
            )
            previous = winners.get(entity_id)
            if (
                previous is None
                or record[0] > previous[0]
                or (
                    record[0] == previous[0]
                    and record[2] == "row"
                    and previous[2] == "ledger"
                )
            ):
                winners[entity_id] = record
        for _event_at, _cursor_id, source, item in page:
            entity_id = str(item.id) if source == "row" else str(item.entity_id)
            winner = winners.get(entity_id)
            if winner is None or winner[3] is not item:
                continue
            if source == "row":
                if getattr(item, "deleted_at", None) is None:
                    changes.append(item.to_dict())
                else:
                    tombstone = {
                        "id": entity_id,
                        "deleted_at": _iso(item.deleted_at),
                    }
                    deletion_batch_id = getattr(item, "deletion_batch_id", None)
                    if deletion_batch_id:
                        tombstone["deletion_batch_id"] = str(deletion_batch_id)
                    tombstones.append(tombstone)
            else:
                # A ledger event can contain a malformed/non-UUID entity id
                # from older integrations.  It is still safe to expose its
                # opaque tombstone; the mobile cache validates IDs at apply.
                tombstones.append(
                    {
                        "id": entity_id,
                        "deleted_at": _iso(item.event_at),
                        "deletion_batch_id": (
                            str(item.batch_id) if item.batch_id is not None else None
                        ),
                    }
                )

        payload = {
            "changes": changes,
            "tombstones": tombstones,
            "cursor": None,
        }
        if len(stream) > task_limit and page:
            event_at, cursor_id, _source, _item = page[-1]
            payload["cursor"] = _encode_general_cursor("tasks", event_at, cursor_id)
        return payload

    # A purged task no longer has a row for the ordinary table query.  Keep
    # the append-only deletion ledger as the durable sync source so a client
    # that was offline across the physical purge still receives a tombstone
    # instead of recreating the stale task on its next push.
    ledger_stmt = select(ContentDeletionEvent).where(
        ContentDeletionEvent.entity_type == "task",
        ContentDeletionEvent.project_id.in_(project_ids),
        ContentDeletionEvent.action.in_(("deleted", "purged", "permanent_deleted")),
    )
    if since:
        ledger_stmt = ledger_stmt.where(ContentDeletionEvent.event_at > since)
    if general_pagination:
        if general_snapshot_at is None:
            raise ValueError("General sync snapshot is missing")
        ledger_stmt = ledger_stmt.where(
            ContentDeletionEvent.event_at <= general_snapshot_at
        )
        decoded_cursor = _decode_general_cursor(general_cursor, "tasks")
        if decoded_cursor:
            cursor_time, cursor_id = decoded_cursor
            ledger_stmt = ledger_stmt.where(
                or_(
                    ContentDeletionEvent.event_at < cursor_time,
                    and_(
                        ContentDeletionEvent.event_at == cursor_time,
                        ContentDeletionEvent.id < cursor_id,
                    ),
                )
            )
    ledger_stmt = ledger_stmt.order_by(
        ContentDeletionEvent.event_at.desc(), ContentDeletionEvent.id.desc()
    ).limit(SYNC_PULL_LIMITS["tasks"])
    ledger_result = await session.execute(ledger_stmt)
    latest_by_id: dict[str, ContentDeletionEvent] = {}
    for event in ledger_result.scalars().all():
        entity_id = str(event.entity_id)
        if entity_id not in latest_by_id:
            latest_by_id[entity_id] = event

    known_ids = {
        str(item.get("id"))
        for item in (*payload.get("changes", []), *payload.get("tombstones", []))
        if item.get("id") is not None
    }
    for entity_id, event in latest_by_id.items():
        if entity_id in known_ids:
            continue
        payload.setdefault("tombstones", []).append(
            {
                "id": entity_id,
                "deleted_at": _iso(event.event_at),
                "deletion_batch_id": (
                    str(event.batch_id) if event.batch_id is not None else None
                ),
            }
        )
    return payload


async def _pull_occurrences(
    session: AsyncSession,
    project_ids: list[UUID],
    since: Optional[datetime],
    *,
    general_pagination: bool = False,
    general_cursor: Optional[str] = None,
    general_snapshot_at: Optional[datetime] = None,
) -> dict[str, Any]:
    if not project_ids:
        return _split_changes([])
    stmt = (
        select(TaskOccurrence)
        .join(Task)
        .options(
            selectinload(TaskOccurrence.task).selectinload(Task.project),
            selectinload(TaskOccurrence.task)
            .selectinload(Task.task_tags)
            .selectinload(TaskTag.tag),
        )
        .where(Task.project_id.in_(project_ids))
    )
    if general_pagination:
        if general_snapshot_at is None:
            raise ValueError("General sync snapshot is missing")
        stmt = _general_page_statement(
            stmt,
            TaskOccurrence,
            table="task_occurrences",
            since=since,
            snapshot_at=general_snapshot_at,
            cursor=general_cursor,
            limit=GENERAL_SYNC_PAGE_LIMITS["task_occurrences"],
        )
    else:
        if since:
            stmt = stmt.where(
                or_(TaskOccurrence.updated_at > since, TaskOccurrence.deleted_at > since)
            )
        stmt = stmt.order_by(TaskOccurrence.updated_at.desc()).limit(
            SYNC_PULL_LIMITS["task_occurrences"]
        )
    result = await session.execute(stmt)
    rows = list(result.scalars().unique().all())
    if general_pagination:
        return _general_page_payload(
            rows,
            table="task_occurrences",
            limit=GENERAL_SYNC_PAGE_LIMITS["task_occurrences"],
            splitter=_split_changes,
            event_getter=_row_general_event_at,
        )
    return _split_changes(rows)


async def _pull_time_entries(
    session: AsyncSession,
    project_ids: list[UUID],
    since: Optional[datetime],
    *,
    general_pagination: bool = False,
    general_cursor: Optional[str] = None,
    general_snapshot_at: Optional[datetime] = None,
) -> dict[str, Any]:
    if not project_ids:
        return _split_changes([])
    stmt = (
        select(TimeEntry)
        .join(Task)
        .options(
            selectinload(TimeEntry.task).selectinload(Task.project),
            selectinload(TimeEntry.user),
            selectinload(TimeEntry.occurrence),
        )
        .where(Task.project_id.in_(project_ids))
    )
    if general_pagination:
        if general_snapshot_at is None:
            raise ValueError("General sync snapshot is missing")
        stmt = _general_page_statement(
            stmt,
            TimeEntry,
            table="time_entries",
            since=since,
            snapshot_at=general_snapshot_at,
            cursor=general_cursor,
            limit=GENERAL_SYNC_PAGE_LIMITS["time_entries"],
        )
    else:
        if since:
            stmt = stmt.where(or_(TimeEntry.updated_at > since, TimeEntry.deleted_at > since))
        stmt = stmt.order_by(TimeEntry.updated_at.desc()).limit(
            SYNC_PULL_LIMITS["time_entries"]
        )
    result = await session.execute(stmt)
    rows = list(result.scalars().unique().all())
    if general_pagination:
        return _general_page_payload(
            rows,
            table="time_entries",
            limit=GENERAL_SYNC_PAGE_LIMITS["time_entries"],
            splitter=_split_changes,
            event_getter=_row_general_event_at,
        )
    return _split_changes(rows)


def _max_datetime_expression(values: list[Any]) -> Any:
    """Return a NULL-free maximum without expanding a nested CASE tree.

    PostgreSQL's ``GREATEST`` and the SQLite test adapter both ignore NULL
    arguments, while the explicit ``coalesce`` keeps the behavior consistent
    for any dialect that does not.  A nested CASE implementation grows
    exponentially once the status dependency clocks are added and can exceed
    SQLite's bound-variable limit.
    """

    normalized = [func.coalesce(value, literal(datetime.min)) for value in values]
    return func.greatest(*normalized)


def _conversation_dependency_event_values() -> list[Any]:
    """Return status-source clocks correlated to the outer session row.

    Status transitions are intentionally not written back to
    ``conversation_sessions``.  These scalar aggregates keep the existing
    session pull contract while making a Run/dialogue-only mutation visible to
    both the legacy ``since`` poll and bounded general pagination.
    """

    run_scope = AgentRun.session_id == ConversationSession.id
    dialogue_scope = or_(
        AgentDialogue.root_session_id == ConversationSession.id,
        AgentDialogue.worker_session_id == ConversationSession.id,
    )
    input_scope = or_(
        AgentDialogue.root_session_id == ConversationSession.id,
        and_(
            AgentDialogue.worker_session_id == ConversationSession.id,
            AgentRun.session_id == AgentDialogue.worker_session_id,
        ),
    )

    def aggregate(columns: tuple[Any, ...], *, source: Any, where: Any, joins=()) -> Any:
        statement = select(func.max(_max_datetime_expression(list(columns)))).select_from(source)
        for join in joins:
            target, on_clause, isouter = join
            statement = statement.join(target, on_clause, isouter=isouter)
        return statement.where(where).correlate(ConversationSession).scalar_subquery()

    return [
        aggregate(
            (
                AgentRun.created_at,
                AgentRun.updated_at,
                AgentRun.last_event_at,
                AgentRun.ended_at,
            ),
            source=AgentRun,
            where=run_scope,
        ),
        aggregate(
            (AgentDialogue.created_at, AgentDialogue.updated_at),
            source=AgentDialogue,
            where=dialogue_scope,
        ),
        aggregate(
            (AgentDialogueInput.created_at, AgentDialogueInput.updated_at),
            source=AgentDialogueInput,
            where=input_scope,
            joins=(
                (
                    AgentDialogue,
                    AgentDialogue.id == AgentDialogueInput.dialogue_id,
                    False,
                ),
                (
                    AgentRun,
                    AgentRun.id == AgentDialogueInput.source_run_id,
                    True,
                ),
            ),
        ),
        aggregate(
            (
                ConversationMessage.created_at,
                ConversationMessage.updated_at,
                ConversationMessage.deleted_at,
            ),
            source=ConversationMessage,
            where=ConversationMessage.session_id == ConversationSession.id,
        ),
    ]


def _conversation_session_event_expression() -> Any:
    """Return one effective mutation clock for session sync.

    The dependency clocks are additive.  Existing session timestamps remain in
    the expression so old rows and read/pin mutations preserve their ordering.
    """

    values = [
        ConversationSession.last_activity,
        ConversationSession.last_read_at,
        ConversationSession.pin_updated_at,
        ConversationSession.agent_binding_updated_at,
        ConversationSession.session_start,
        ConversationSession.deleted_at,
    ]
    values.extend(_conversation_dependency_event_values())
    return _max_datetime_expression(values)


def _conversation_rows_with_event(result: Any) -> list[ConversationSession]:
    """Unpack ORM rows and retain the SQL event key for cursor serialization."""

    rows: list[ConversationSession] = []
    for item in result.all():
        try:
            row = item[0]
            event_at = item[1]
        except (IndexError, KeyError, TypeError):
            row = item
            event_at = None
        setattr(row, _CONVERSATION_EVENT_ATTR, event_at)
        rows.append(row)
    return rows


async def _project_sync_conversation_statuses(
    session: AsyncSession,
    session_ids: list[str],
    last_read_at_by_session: dict[str, Any],
) -> dict[str, ConversationStatusProjection]:
    """Project one visible batch using the canonical history status service."""

    if not session_ids:
        return {}
    return await project_conversation_statuses(
        session_ids,
        last_read_at_by_session=last_read_at_by_session,
        db_session=session,
    )


async def _apply_sync_conversation_statuses(
    session: AsyncSession,
    payloads: list[dict[str, Any]],
) -> None:
    """Decorate pull/push session payloads with one shared status batch."""

    if not payloads:
        return
    conversation_ids = list(
        dict.fromkeys(
            str(payload.get("id"))
            for payload in payloads
            if payload.get("id")
        )
    )
    read_at_by_session = {
        str(payload.get("id")): payload.get("last_read_at")
        for payload in payloads
        if payload.get("id")
    }
    projections = await _project_sync_conversation_statuses(
        session,
        conversation_ids,
        read_at_by_session,
    )
    for payload in payloads:
        _apply_conversation_status_projection(
            payload,
            projections.get(str(payload.get("id"))),
        )


async def _pull_conversation_sessions(
    session: AsyncSession,
    user_id: UUID,
    since: Optional[datetime],
    *,
    project_ids: Optional[list[UUID]] = None,
    general_pagination: bool = False,
    general_cursor: Optional[str] = None,
    general_snapshot_at: Optional[datetime] = None,
) -> dict[str, Any]:
    visibility = _conversation_visibility_filter(user_id, project_ids or [])

    def build_statement() -> Any:
        stmt = select(ConversationSession).where(visibility)
        session_event = _conversation_session_event_expression()
        if general_pagination:
            if general_snapshot_at is None:
                raise ValueError("General sync snapshot is missing")
            stmt = _general_page_statement(
                stmt,
                ConversationSession,
                table="conversation_sessions",
                since=since,
                snapshot_at=general_snapshot_at,
                cursor=general_cursor,
                limit=GENERAL_SYNC_PAGE_LIMITS["conversation_sessions"],
                event_expression=session_event,
            )
        else:
            if since:
                stmt = stmt.where(session_event > since)
            stmt = stmt.order_by(
                session_event.desc(), ConversationSession.id.desc()
            ).limit(SYNC_PULL_LIMITS["conversation_sessions"])
        # Retain the exact SQL event key so a status-only mutation gets a
        # matching cursor and wire ``updated_at`` value.
        return stmt.add_columns(session_event.label(_CONVERSATION_EVENT_ATTR))

    result = await session.execute(build_statement())
    rows = _conversation_rows_with_event(result)
    await attach_private_root_metadata(session, rows)

    status_ids: list[str] = []
    last_read_at_by_session: dict[str, Any] = {}
    for row in rows:
        session_id = str(row.id)
        if row.deleted_at is None:
            status_ids.append(session_id)
            last_read_at_by_session[session_id] = row.last_read_at
    status_projections = await _project_sync_conversation_statuses(
        session,
        status_ids,
        last_read_at_by_session,
    )

    def payload_for(row: Any) -> dict[str, Any]:
        session_id = str(row.id)
        return _conversation_session_payload(
            row,
            status_projection=status_projections.get(session_id),
        )

    def split_page(page: list[ConversationSession]) -> dict[str, Any]:
        return {
            "changes": [
                payload_for(row)
                for row in page
                if row.deleted_at is None
            ],
            "tombstones": [
                {"id": str(row.id), "deleted_at": _iso(row.deleted_at)}
                for row in page
                if row.deleted_at is not None
            ],
            "cursor": None,
        }

    page_rows = (
        rows[: GENERAL_SYNC_PAGE_LIMITS["conversation_sessions"]]
        if general_pagination
        else rows
    )
    page_payload = split_page(page_rows)

    payload: dict[str, Any] = {
        "changes": page_payload["changes"],
        "tombstones": page_payload["tombstones"],
        "cursor": None,
    }
    if general_pagination:
        payload = _general_page_payload(
            rows,
            table="conversation_sessions",
            limit=GENERAL_SYNC_PAGE_LIMITS["conversation_sessions"],
            splitter=split_page,
            event_getter=_conversation_row_event_at,
        )
        # Authoritative reconciliation is safe only on the terminal page;
        # reconciling against a partial page would delete valid local rows.
        if payload.get("cursor") is None:
            active_ids_result = await session.execute(
                select(ConversationSession.id).where(
                    visibility,
                    ConversationSession.deleted_at.is_(None),
                )
            )
            payload["authoritative_ids"] = [
                str(item) for item in active_ids_result.scalars().all()
            ]
        return payload

    active_ids_result = await session.execute(
        select(ConversationSession.id).where(
            visibility,
            ConversationSession.deleted_at.is_(None),
        )
    )
    payload["authoritative_ids"] = [str(item) for item in active_ids_result.scalars().all()]
    return payload


async def _pull_conversation_messages(
    session: AsyncSession,
    user_id: UUID,
    since: Optional[datetime],
    *,
    project_ids: Optional[list[UUID]] = None,
    general_pagination: bool = False,
    general_cursor: Optional[str] = None,
    general_snapshot_at: Optional[datetime] = None,
) -> dict[str, Any]:
    stmt = (
        select(ConversationMessage)
        .join(
            ConversationSession,
            ConversationMessage.session_id == ConversationSession.id,
        )
        .where(_conversation_visibility_filter(user_id, project_ids or []))
    )
    if general_pagination:
        if general_snapshot_at is None:
            raise ValueError("General sync snapshot is missing")
        stmt = _general_page_statement(
            stmt,
            ConversationMessage,
            table="conversation_messages",
            since=since,
            snapshot_at=general_snapshot_at,
            cursor=general_cursor,
            limit=GENERAL_SYNC_PAGE_LIMITS["conversation_messages"],
        )
    else:
        if since:
            stmt = stmt.where(
                or_(
                    ConversationMessage.updated_at > since,
                    ConversationMessage.deleted_at > since,
                )
            )
        stmt = stmt.order_by(ConversationMessage.updated_at.desc()).limit(
            SYNC_PULL_LIMITS["conversation_messages"]
        )
    result = await session.execute(stmt)
    rows = list(result.scalars().unique().all())
    if general_pagination:
        return _general_page_payload(
            rows,
            table="conversation_messages",
            limit=GENERAL_SYNC_PAGE_LIMITS["conversation_messages"],
            splitter=_split_changes,
            event_getter=_row_general_event_at,
        )
    return _split_changes(rows)


async def _pull_record_tables(
    session: AsyncSession,
    project_ids: list[UUID],
    since: Optional[datetime],
    *,
    general_pagination: bool = False,
    general_cursor: Optional[str] = None,
    general_snapshot_at: Optional[datetime] = None,
) -> dict[str, Any]:
    if not project_ids:
        return _split_record_changes([], _record_table_payload)
    stmt = select(RecordTable).where(RecordTable.project_id.in_(project_ids))
    if general_pagination:
        if general_snapshot_at is None:
            raise ValueError("General sync snapshot is missing")
        stmt = _general_page_statement(
            stmt,
            RecordTable,
            table="record_tables",
            since=since,
            snapshot_at=general_snapshot_at,
            cursor=general_cursor,
            limit=GENERAL_SYNC_PAGE_LIMITS["record_tables"],
        )
    else:
        if since:
            stmt = stmt.where(
                or_(RecordTable.updated_at > since, RecordTable.deleted_at > since)
            )
        stmt = stmt.order_by(RecordTable.updated_at.desc()).limit(
            SYNC_PULL_LIMITS["record_tables"]
        )
    result = await session.execute(stmt)
    rows = list(result.scalars().all())
    if general_pagination:
        return _general_page_payload(
            rows,
            table="record_tables",
            limit=GENERAL_SYNC_PAGE_LIMITS["record_tables"],
            splitter=lambda page: _split_record_changes(page, _record_table_payload),
            event_getter=_row_general_event_at,
        )
    return _split_record_changes(rows, _record_table_payload)


async def _pull_record_fields(
    session: AsyncSession,
    project_ids: list[UUID],
    since: Optional[datetime],
    *,
    general_pagination: bool = False,
    general_cursor: Optional[str] = None,
    general_snapshot_at: Optional[datetime] = None,
) -> dict[str, Any]:
    if not project_ids:
        return _split_record_changes([], _record_field_payload)
    stmt = select(RecordField).join(RecordTable).where(
        RecordTable.project_id.in_(project_ids)
    )
    if general_pagination:
        if general_snapshot_at is None:
            raise ValueError("General sync snapshot is missing")
        stmt = _general_page_statement(
            stmt,
            RecordField,
            table="record_fields",
            since=since,
            snapshot_at=general_snapshot_at,
            cursor=general_cursor,
            limit=GENERAL_SYNC_PAGE_LIMITS["record_fields"],
        )
    else:
        if since:
            stmt = stmt.where(
                or_(RecordField.updated_at > since, RecordField.deleted_at > since)
            )
        stmt = stmt.order_by(RecordField.updated_at.desc()).limit(
            SYNC_PULL_LIMITS["record_fields"]
        )
    result = await session.execute(stmt)
    rows = list(result.scalars().all())
    if general_pagination:
        return _general_page_payload(
            rows,
            table="record_fields",
            limit=GENERAL_SYNC_PAGE_LIMITS["record_fields"],
            splitter=lambda page: _split_record_changes(page, _record_field_payload),
            event_getter=_row_general_event_at,
        )
    return _split_record_changes(rows, _record_field_payload)


async def _pull_record_rows(
    session: AsyncSession,
    project_ids: list[UUID],
    since: Optional[datetime],
    *,
    general_pagination: bool = False,
    general_cursor: Optional[str] = None,
    general_snapshot_at: Optional[datetime] = None,
) -> dict[str, Any]:
    if not project_ids:
        return _split_record_changes([], _record_row_payload)
    stmt = select(RecordRow).where(RecordRow.project_id.in_(project_ids))
    if general_pagination:
        if general_snapshot_at is None:
            raise ValueError("General sync snapshot is missing")
        stmt = _general_page_statement(
            stmt,
            RecordRow,
            table="record_rows",
            since=since,
            snapshot_at=general_snapshot_at,
            cursor=general_cursor,
            limit=GENERAL_SYNC_PAGE_LIMITS["record_rows"],
        )
    else:
        if since:
            stmt = stmt.where(or_(RecordRow.updated_at > since, RecordRow.deleted_at > since))
        stmt = stmt.order_by(RecordRow.updated_at.desc()).limit(
            SYNC_PULL_LIMITS["record_rows"]
        )
    result = await session.execute(stmt)
    rows = list(result.scalars().all())
    if general_pagination:
        return _general_page_payload(
            rows,
            table="record_rows",
            limit=GENERAL_SYNC_PAGE_LIMITS["record_rows"],
            splitter=lambda page: _split_record_changes(page, _record_row_payload),
            event_getter=_row_general_event_at,
        )
    return _split_record_changes(rows, _record_row_payload)


async def _pull_table(
    table: str,
    session: AsyncSession,
    *,
    user_id: UUID,
    project_ids: list[UUID],
    since: Optional[datetime],
    docs_docs_library_id: Optional[UUID] = None,
    docs_digests: Optional[dict[str, str]] = None,
    docs_cursors: Optional[dict[str, str]] = None,
    docs_pagination: bool = False,
    force_docs_full: bool = False,
    docs_reconcile: bool = True,
    docs_snapshot_token: Optional[str] = None,
    docs_scope_revision: Optional[str] = None,
    docs_project_id: Optional[UUID] = None,
    docs_accessible_project_ids: Optional[list[UUID]] = None,
    general_pagination: bool = False,
    general_cursor: Optional[str] = None,
    general_snapshot_at: Optional[datetime] = None,
) -> dict[str, Any]:
    if table in DOCS_SYNC_TABLES:
        if docs_docs_library_id is None:
            return {"changes": [], "tombstones": [], "cursor": None}
        return await pull_docs_table(
            table,
            session,
            docs_library_id=docs_docs_library_id,
            accessible_project_ids=(
                docs_accessible_project_ids
                if docs_accessible_project_ids is not None
                else project_ids
            ),
            scope_project_id=docs_project_id,
            since=since,
            client_digest=(docs_digests or {}).get(table),
            cursor=(docs_cursors or {}).get(table),
            paginate=docs_pagination,
            force_full=force_docs_full,
            include_authoritative_ids=docs_reconcile,
            user_id=user_id,
            snapshot_token=docs_snapshot_token,
            scope_revision=docs_scope_revision,
        )
    if table == "projects":
        payload = await _pull_projects(
            session,
            project_ids,
            since,
            general_pagination=general_pagination,
            general_cursor=general_cursor,
            general_snapshot_at=general_snapshot_at,
        )
        # Sync retains management/Docs visibility. The mobile operational
        # selectors and task aggregates must use participation, not that ACL.
        participating_ids = {
            str(project_id)
            for project_id in await ProjectRepository.get_participating_project_ids(
                session, user_id
            )
        }
        for project in payload["changes"]:
            project["is_participating"] = str(project["id"]) in participating_ids
        return payload
    if table == "tasks":
        return await _pull_tasks(
            session,
            project_ids,
            since,
            general_pagination=general_pagination,
            general_cursor=general_cursor,
            general_snapshot_at=general_snapshot_at,
        )
    if table == "task_occurrences":
        return await _pull_occurrences(
            session,
            project_ids,
            since,
            general_pagination=general_pagination,
            general_cursor=general_cursor,
            general_snapshot_at=general_snapshot_at,
        )
    if table == "time_entries":
        return await _pull_time_entries(
            session,
            project_ids,
            since,
            general_pagination=general_pagination,
            general_cursor=general_cursor,
            general_snapshot_at=general_snapshot_at,
        )
    if table == "conversation_sessions":
        return await _pull_conversation_sessions(
            session,
            user_id,
            since,
            project_ids=project_ids,
            general_pagination=general_pagination,
            general_cursor=general_cursor,
            general_snapshot_at=general_snapshot_at,
        )
    if table == "conversation_messages":
        return await _pull_conversation_messages(
            session,
            user_id,
            since,
            project_ids=project_ids,
            general_pagination=general_pagination,
            general_cursor=general_cursor,
            general_snapshot_at=general_snapshot_at,
        )
    if table == "record_tables":
        return await _pull_record_tables(
            session,
            project_ids,
            since,
            general_pagination=general_pagination,
            general_cursor=general_cursor,
            general_snapshot_at=general_snapshot_at,
        )
    if table == "record_fields":
        return await _pull_record_fields(
            session,
            project_ids,
            since,
            general_pagination=general_pagination,
            general_cursor=general_cursor,
            general_snapshot_at=general_snapshot_at,
        )
    if table == "record_rows":
        return await _pull_record_rows(
            session,
            project_ids,
            since,
            general_pagination=general_pagination,
            general_cursor=general_cursor,
            general_snapshot_at=general_snapshot_at,
        )
    return {"changes": [], "tombstones": [], "cursor": None}


def _task_updates_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if "project_id" in payload:
        updates["project_id"] = parse_uuid_or_400(
            payload.get("project_id"), "project_id"
        )
    if "title" in payload:
        updates["title"] = payload.get("title")
    if "description" in payload:
        updates["description"] = payload.get("description")
    if "status" in payload:
        updates["status"] = payload.get("status")
    if "priority" in payload:
        updates["priority"] = payload.get("priority")
    if "start_at" in payload:
        updates["start_at"] = _parse_wall_clock_datetime(
            payload.get("start_at"), "start_at"
        )
    if "end_at" in payload:
        updates["end_at"] = _parse_wall_clock_datetime(
            payload.get("end_at"), "end_at"
        )
    if "all_day" in payload:
        updates["all_day"] = payload.get("all_day")
    if "auto_close_on_due" in payload:
        updates["auto_close_on_due"] = payload.get("auto_close_on_due")
    if "reminder_offsets" in payload:
        updates["reminder_offsets"] = payload.get("reminder_offsets")
    if "notifications_enabled" in payload:
        updates["notifications_enabled"] = payload.get("notifications_enabled")
    if "metadata" in payload or "task_metadata" in payload:
        updates["task_metadata"] = _task_metadata_from_payload(payload)
    if "estimated_hours" in payload:
        updates["estimated_hours"] = payload.get("estimated_hours")
    if "parent_task_id" in payload:
        updates["parent_task_id"] = (
            parse_uuid_or_400(payload.get("parent_task_id"), "parent_task_id")
            if payload.get("parent_task_id")
            else None
        )
    if "tag_ids" in payload:
        updates["tag_ids"] = [
            parse_uuid_or_400(value, "tag_id")
            for value in (payload.get("tag_ids") or [])
        ]
    if "assignee_ids" in payload:
        updates["assignee_ids"] = [
            parse_uuid_or_400(value, "assignee_id")
            for value in (payload.get("assignee_ids") or [])
        ]
    return updates


def _task_metadata_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    raw_metadata = payload.get("metadata") or payload.get("task_metadata")
    if not isinstance(raw_metadata, dict):
        return None
    metadata = dict(raw_metadata)
    metadata.pop("mobile_sync_status", None)
    metadata.pop("mobile_sync_error", None)
    return metadata


def _time_entry_values_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": parse_uuid_or_400(payload.get("task_id"), "task_id"),
        "occurrence_id": parse_uuid_or_400(payload.get("occurrence_id"), "occurrence_id")
        if "occurrence_id" in payload
        else None,
        "started_at": _parse_timer_datetime(payload.get("started_at"), "started_at")
        if "started_at" in payload
        else None,
        "ended_at": _parse_timer_datetime(payload.get("ended_at"), "ended_at")
        if "ended_at" in payload
        else None,
        "source": str(payload.get("source") or "mobile"),
        "note": payload.get("note"),
        "entry_metadata": payload.get("metadata") or payload.get("entry_metadata") or {},
    }


def _project_values_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    values = {
        "name": str(payload.get("name") or "").strip(),
        "description": payload.get("description"),
        "aliases": payload.get("aliases") if isinstance(payload.get("aliases"), list) else None,
        "allow_join_requests": payload.get("allow_join_requests"),
        "storage_quota_mb": payload.get("storage_quota_mb"),
        "project_metadata": payload.get("metadata") or payload.get("project_metadata"),
    }
    if "space_id" in payload:
        values["space_id"] = (
            parse_uuid_or_400(payload.get("space_id"), "space_id")
            if payload.get("space_id")
            else None
        )
    if "is_completed" in payload:
        values["is_completed"] = bool(payload.get("is_completed"))
    return values


async def _load_current_task_payload(
    service: TaskManagementService,
    session: AsyncSession,
    task_id: UUID,
    *,
    user_id: UUID,
) -> Optional[dict[str, Any]]:
    # Conflict projections are reads too. Re-check the stored object's ACL
    # after rollback rather than trusting the write check or client payload.
    result = await session.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if task is None:
        return None
    try:
        await service.require_project_permission(
            session, project_id=task.project_id, user_id=user_id, permission="read"
        )
    except TaskManagementError as exc:
        if exc.status_code in (403, 404):
            return None
        raise
    if task.deleted_at is not None:
        return _task_tombstone_payload(task)
    return (await service._load_task(session, task_id)).to_dict()


def _task_tombstone_payload(task: Task) -> dict[str, Any]:
    """Return the canonical sync tombstone for an already-deleted task."""

    return {
        "id": str(task.id),
        "task_id": str(task.id),
        "updated_at": _iso(task.updated_at),
        "deleted_at": _iso(task.deleted_at),
        "deletion_batch_id": (
            str(task.deletion_batch_id)
            if getattr(task, "deletion_batch_id", None)
            else None
        ),
        "idempotent": True,
    }


async def _require_time_entry_permission(
    service: TaskManagementService,
    session: AsyncSession,
    entry: TimeEntry,
    *,
    user_id: UUID,
    permission: str,
) -> None:
    # Include task tombstones for idempotent deletes, and never use the
    # request's task_id to authorize an already-persisted entry.
    result = await session.execute(select(Task).where(Task.id == entry.task_id))
    task = result.scalar_one_or_none()
    if task is None:
        raise TaskManagementError("Task not found", status_code=404)
    await service.require_project_permission(
        session, project_id=task.project_id, user_id=user_id, permission=permission
    )


async def _load_current_time_entry_payload(
    service: TaskManagementService,
    session: AsyncSession,
    entry_id: UUID,
    *,
    user_id: UUID,
) -> Optional[dict[str, Any]]:
    result = await session.execute(select(TimeEntry).where(TimeEntry.id == entry_id))
    entry = result.scalar_one_or_none()
    if entry is None:
        return None
    try:
        await _require_time_entry_permission(
            service, session, entry, user_id=user_id, permission="read"
        )
    except TaskManagementError as exc:
        if exc.status_code in (403, 404):
            return None
        raise
    if entry.deleted_at is not None:
        return {
            "id": str(entry.id),
            "updated_at": _iso(entry.updated_at),
            "deleted_at": _iso(entry.deleted_at),
            "deletion_batch_id": (
                str(entry.deletion_batch_id)
                if getattr(entry, "deletion_batch_id", None)
                else None
            ),
        }
    return await service._serialize_time_entry(session, entry.id)


async def _apply_task_operation(
    service: TaskManagementService,
    session: AsyncSession,
    *,
    user_id: UUID,
    operation: SyncOperation,
    commit: bool = True,
    after_commit: list[Callable[[], None]] | None = None,
) -> dict[str, Any]:
    if not commit and after_commit is None:
        raise ValueError("after_commit is required when commit=False")
    task_id = parse_uuid_or_400(operation.entity_id, "entity_id")
    if task_id is None:
        raise TaskManagementError("entity_id is required", status_code=400)

    existing_result = await session.execute(select(Task).where(Task.id == task_id))
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        # Authorize before stale checks, tombstones, or create replays can
        # reveal server state. The stored project is authoritative.
        await service.require_project_permission(
            session, project_id=existing.project_id, user_id=user_id, permission="write"
        )

    if operation.action == "delete":
        if existing is None:
            # A client may retry a delete after the retention worker has
            # already purged the row.  Keep the operation idempotent while
            # accurately reporting that no server tombstone remains.
            return {
                "id": str(task_id),
                "task_id": str(task_id),
                "updated_at": None,
                "deleted_at": None,
                "deletion_batch_id": None,
                "idempotent": True,
                "purged": True,
            }
        if existing.deleted_at is not None:
            # Do not re-run stale checking for a tombstone.  A repeated offline
            # delete must converge on the server's original batch/timestamp.
            return _task_tombstone_payload(existing)
        _ensure_not_stale(
            existing.updated_at,
            operation.base_updated_at,
            reason_code="task_cas_stale",
        )
        return await service.delete_task(
            session, user_id=user_id, task_id=task_id, commit=commit
        )

    if operation.action == "restore":
        raw_batch_id = operation.payload.get("deletion_batch_id")
        batch_id = (
            parse_uuid_or_400(raw_batch_id, "deletion_batch_id")
            if raw_batch_id
            else None
        )
        return await service.restore_task(
            session,
            user_id=user_id,
            task_id=task_id,
            deletion_batch_id=batch_id,
            commit=commit,
        )

    if operation.action == "create":
        payload = operation.payload
        title = str(payload.get("title") or "").strip()
        if not title:
            raise TaskManagementError("title is required", status_code=400)
        if existing is not None and existing.deleted_at is not None:
            # A normal create is never an implicit restore.  The explicit
            # restore operation carries the batch guard and ACL checks.
            raise TaskManagementError(
                "Task is deleted; use restore before creating it again",
                status_code=409,
                detail={"code": "task_tombstoned"},
            )
        if existing is None:
            ledger_result = await session.execute(
                select(ContentDeletionEvent)
                .where(
                    ContentDeletionEvent.entity_type == "task",
                    ContentDeletionEvent.entity_id == str(task_id),
                    ContentDeletionEvent.action.in_(
                        ("deleted", "purged", "permanent_deleted")
                    ),
                )
                .order_by(ContentDeletionEvent.event_at.desc())
                .limit(1)
            )
            if ledger_result.scalar_one_or_none() is not None:
                raise TaskManagementError(
                    "Task was permanently removed; stale create is rejected",
                    status_code=409,
                    detail={"code": "task_purged_tombstone"},
                )
        if existing is not None and existing.deleted_at is None:
            _ensure_not_stale(
                existing.updated_at,
                operation.base_updated_at,
                reason_code="task_cas_stale",
            )
            return await service.update_task(
                session,
                user_id=user_id,
                task_id=task_id,
                updates=_task_updates_from_payload(payload),
                commit=commit,
            )
        try:
            task_create_payload = CreateTaskPayload.model_validate(
                {
                    "title": title,
                    "recurrence_rrule": payload.get("recurrence_rrule"),
                    "recurrence_timezone": payload.get(
                        "recurrence_timezone", DEFAULT_TASK_TIMEZONE
                    ),
                }
            )
        except ValidationError as exc:
            raise TaskManagementError(
                "Invalid task recurrence payload", status_code=400
            ) from exc

        created = await service.create_task(
            session,
            user_id=user_id,
            task_id=task_id,
            project_id=parse_uuid_or_400(payload.get("project_id"), "project_id"),
            title=title,
            description=payload.get("description"),
            status=str(payload.get("status") or "open"),
            priority=payload.get("priority"),
            start_at=_parse_wall_clock_datetime(payload.get("start_at"), "start_at"),
            end_at=_parse_wall_clock_datetime(payload.get("end_at"), "end_at"),
            all_day=bool(payload.get("all_day") or False),
            auto_close_on_due=bool(payload.get("auto_close_on_due") or False),
            reminder_offsets=payload.get("reminder_offsets"),
            notifications_enabled=payload.get("notifications_enabled"),
            estimated_hours=payload.get("estimated_hours"),
            parent_task_id=(
                parse_uuid_or_400(payload.get("parent_task_id"), "parent_task_id")
                if payload.get("parent_task_id")
                else None
            ),
            tag_ids=[
                parse_uuid_or_400(value, "tag_id")
                for value in (payload.get("tag_ids") or [])
            ],
            assignee_ids=[
                parse_uuid_or_400(value, "assignee_id")
                for value in (payload.get("assignee_ids") or [])
            ],
            recurrence_rrule=task_create_payload.recurrence_rrule,
            recurrence_timezone=task_create_payload.recurrence_timezone,
            task_metadata=_task_metadata_from_payload(payload),
            source=str(payload.get("source") or "mobile"),
            commit=commit,
        )
        return created

    if operation.action == "update":
        current = await service._load_task(session, task_id)
        _ensure_not_stale(
            current.updated_at,
            operation.base_updated_at,
            reason_code="task_cas_stale",
        )
        return await service.update_task(
            session,
            user_id=user_id,
            task_id=task_id,
            updates=_task_updates_from_payload(operation.payload),
            commit=commit,
        )

    raise TaskManagementError("Unsupported task sync action", status_code=400)


async def _apply_conversation_session_operation(
    session: AsyncSession,
    *,
    user_id: UUID,
    user_info: dict[str, Any],
    operation: SyncOperation,
    commit: bool = True,
    after_commit: list[Callable[[], None]] | None = None,
) -> dict[str, Any]:
    """Apply native conversation pin updates and durable offline deletes."""
    if not commit and after_commit is None:
        raise ValueError("after_commit is required when commit=False")

    conversation_id = parse_uuid_or_400(operation.entity_id, "entity_id")
    if conversation_id is None:
        raise TaskManagementError("entity_id is required", status_code=400)

    if operation.action not in {"update", "delete"}:
        raise TaskManagementError(
            "Conversation sync supports update/delete only", status_code=400
        )
    if operation.action == "update":
        if set(operation.payload) != {"is_pinned"}:
            raise TaskManagementError(
                "Conversation sync accepts is_pinned only for update",
                status_code=400,
            )
        is_pinned = operation.payload.get("is_pinned")
        if not isinstance(is_pinned, bool):
            raise TaskManagementError("is_pinned must be boolean", status_code=400)
    else:
        if operation.payload:
            raise TaskManagementError(
                "Conversation delete payload must be empty", status_code=400
            )
        is_pinned = None

    existing = (
        await session.execute(
            select(ConversationSession)
            .options(selectinload(ConversationSession.participants))
            .where(ConversationSession.id == conversation_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing is None:
        if operation.action == "delete":
            return {
                "id": str(conversation_id),
                "deleted_at": None,
                "idempotent": True,
                "purged": True,
            }
        raise TaskManagementError("Conversation not found", status_code=404)

    await attach_private_root_metadata(session, [existing])

    if operation.action == "update" and existing.deleted_at is not None:
        raise TaskManagementError("Conversation not found", status_code=404)

    actor_id = str(user_id)
    can_manage = (
        str(user_info.get("role") or "") == "admin"
        or str(existing.user_id) == actor_id
        or any(
            participant.participant_type == "user"
            and str(participant.participant_id) == actor_id
            and participant.status == "joined"
            and participant.role in {"owner", "admin"}
            for participant in (existing.participants or [])
        )
    )
    if not can_manage:
        raise TaskManagementError("Conversation management denied", status_code=403)
    if existing.project_id is not None:
        allowed = await ProjectRepository.has_permission(
            session,
            existing.project_id,
            user_id,
            "write",
        )
        if not allowed:
            raise TaskManagementError("Project permission denied", status_code=403)

    if operation.action == "delete":
        try:
            await guard_private_root_operation(
                session,
                existing,
                operation="delete",
                actor_user_id=user_id,
            )
        except AgentRootLifecycleError as exc:
            raise TaskManagementError(
                str(exc),
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    if operation.action == "delete":
        if existing.deleted_at is None:
            existing.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)
            if commit:
                await session.commit()
                await session.refresh(existing)
                release_session_agent_team_registry(
                    str(existing.user_id) if existing.user_id is not None else None,
                    str(existing.id),
                )
            else:
                await session.flush()
                await session.refresh(existing)
                after_commit.append(
                    functools.partial(
                        release_session_agent_team_registry,
                        str(existing.user_id) if existing.user_id is not None else None,
                        str(existing.id),
                    )
                )
        payload = _conversation_session_payload(existing)
        payload["idempotent"] = True
        return payload

    _ensure_not_stale(existing.pin_updated_at, operation.base_updated_at)
    changed_at = datetime.utcnow()
    existing.is_pinned = is_pinned
    existing.pinned_at = changed_at if is_pinned else None
    existing.pin_updated_at = changed_at
    if commit:
        await session.commit()
    else:
        await session.flush()
    await session.refresh(existing)
    return _conversation_session_payload(existing)


async def _apply_time_entry_operation(
    service: TaskManagementService,
    session: AsyncSession,
    *,
    user_id: UUID,
    operation: SyncOperation,
    commit: bool = True,
    after_commit: list[Callable[[], None]] | None = None,
) -> dict[str, Any]:
    if not commit and after_commit is None:
        raise ValueError("after_commit is required when commit=False")
    entry_id = parse_uuid_or_400(operation.entity_id, "entity_id")
    if entry_id is None:
        raise TaskManagementError("entity_id is required", status_code=400)

    values = _time_entry_values_from_payload(operation.payload)
    existing_result = await session.execute(select(TimeEntry).where(TimeEntry.id == entry_id))
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        await _require_time_entry_permission(
            service, session, existing, user_id=user_id, permission="write"
        )

    if operation.action == "delete":
        if existing is None:
            return {
                "id": str(entry_id),
                "updated_at": None,
                "deleted_at": None,
                "deletion_batch_id": None,
                "idempotent": True,
                "purged": True,
            }
        if existing.deleted_at is not None:
            return {
                "id": str(existing.id),
                "updated_at": _iso(existing.updated_at),
                "deleted_at": _iso(existing.deleted_at),
                "deletion_batch_id": (
                    str(existing.deletion_batch_id)
                    if getattr(existing, "deletion_batch_id", None)
                    else None
                ),
                "idempotent": True,
            }
        _ensure_not_stale(existing.updated_at, operation.base_updated_at)
        await service.delete_time_entry(
            session, user_id=user_id, entry_id=entry_id, commit=commit
        )
        # The deleted row is intentionally hidden by the live serializer, so
        # return the exact in-memory tombstone rather than reloading it with a
        # ``deleted_at IS NULL`` predicate.
        return {
            "id": str(existing.id),
            "updated_at": _iso(existing.updated_at),
            "deleted_at": _iso(existing.deleted_at),
            "deletion_batch_id": (
                str(existing.deletion_batch_id)
                if getattr(existing, "deletion_batch_id", None)
                else None
            ),
            "idempotent": False,
        }

    if operation.action == "create":
        task_id = values["task_id"]
        started_at = values["started_at"]
        if task_id is None:
            raise TaskManagementError("task_id is required", status_code=400)
        if started_at is None:
            raise TaskManagementError("started_at is required", status_code=400)

        task = await service._load_task(session, task_id)
        await service.require_project_permission(
            session, project_id=task.project_id, user_id=user_id, permission="write"
        )

        if existing is not None:
            if existing.deleted_at is not None:
                raise TaskManagementError("Time entry is deleted", status_code=409)
            _ensure_not_stale(existing.updated_at, operation.base_updated_at)
            return await service._serialize_time_entry(session, existing.id)

        ended_at = values["ended_at"]
        if ended_at is not None and ended_at <= started_at:
            raise TaskManagementError("ended_at must be after started_at", status_code=400)
        if ended_at is None:
            active_result = await session.execute(
                select(TimeEntry).where(
                    TimeEntry.user_id == user_id,
                    TimeEntry.ended_at.is_(None),
                    TimeEntry.deleted_at.is_(None),
                    TimeEntry.id != entry_id,
                ).with_for_update()
            )
            active_entries = list(active_result.scalars().all())
            # An older offline start cannot stop a newer server timer. Keep
            # both the server timer and outbox operation intact for resolution.
            if any(entry.started_at >= started_at for entry in active_entries):
                raise TaskManagementError(
                    "A newer timer is already active on the server", status_code=409
                )
            for active_entry in active_entries:
                active_entry.ended_at = started_at
                active_entry.updated_at = datetime.utcnow()
            # Release the unique active-user slot before inserting its successor.
            await session.flush()

        entry = TimeEntry(
            id=entry_id,
            task_id=task.id,
            occurrence_id=values["occurrence_id"],
            user_id=user_id,
            started_at=started_at,
            ended_at=values["ended_at"],
            source=values["source"],
            note=values["note"],
            entry_metadata=values["entry_metadata"],
        )
        session.add(entry)
        if values["ended_at"] is None:
            task.status = "in_progress"
        if commit:
            await session.commit()
        else:
            await session.flush()
        return await service._serialize_time_entry(session, entry.id)

    if operation.action == "update":
        if existing is None or existing.deleted_at is not None:
            raise TaskManagementError("Time entry not found", status_code=404)
        _ensure_not_stale(existing.updated_at, operation.base_updated_at)
        return await service.update_time_entry(
            session,
            user_id=user_id,
            entry_id=entry_id,
            started_at=values["started_at"],
            ended_at=values["ended_at"] if "ended_at" in operation.payload else None,
            note=values["note"] if "note" in operation.payload else None,
            commit=commit,
        )

    raise TaskManagementError("Unsupported time entry sync action", status_code=400)


async def _apply_project_operation(
    service: TaskManagementService,
    session: AsyncSession,
    *,
    user_id: UUID,
    user_info: Optional[dict[str, Any]] = None,
    operation: SyncOperation,
    workspace_root: str | os.PathLike[str] | None = None,
    commit: bool = True,
    after_commit: list[Callable[[], None]] | None = None,
) -> dict[str, Any]:
    """Project sync operation を適用する。

    ``commit=False`` の delete は、呼出元が同じ実効rootで
    ``project_operation_lock`` を保持していること。
    """
    if not commit and after_commit is None:
        raise ValueError("after_commit is required when commit=False")
    project_id = parse_uuid_or_400(operation.entity_id, "entity_id")
    if project_id is None:
        raise TaskManagementError("entity_id is required", status_code=400)

    existing = await ProjectRepository.get_by_id(session, project_id)
    values = _project_values_from_payload(operation.payload)

    if operation.action == "delete":
        if existing is None:
            return {"id": str(project_id), "deleted_at": datetime.utcnow().isoformat()}
        await service.require_project_permission(
            session, project_id=project_id, user_id=user_id, permission="delete"
        )
        _ensure_not_stale(existing.updated_at, operation.base_updated_at)
        if commit:
            await ProjectRepository.delete_project(
                session,
                project_id,
                delete_workspace=True,
                workspace_root=workspace_root,
            )
        else:
            await ProjectRepository._delete_project_unlocked(
                session,
                project_id,
                delete_workspace=True,
                workspace_root=get_workspaces_root(workspace_root),
                commit=False,
                after_commit=after_commit,
            )
        return {"id": str(project_id), "deleted_at": datetime.utcnow().isoformat()}

    if operation.action == "create":
        name = values["name"]
        if not name:
            raise TaskManagementError("name is required", status_code=400)
        if existing is not None and existing.deleted_at is None:
            await service.require_project_permission(
                session, project_id=project_id, user_id=user_id, permission="write"
            )
            _ensure_not_stale(existing.updated_at, operation.base_updated_at)
            if (
                "name" in values
                and existing.is_completed
                and str(values["name"] or "").strip()
                != str(existing.name or "").strip()
            ):
                raise TaskManagementError(
                    "完了済みProjectの名前は変更できません。再開してから変更してください",
                    status_code=409,
                )
            if "space_id" in values and values["space_id"] is not None:
                from ..services.space_access import can_write_space

                can_write, space = await can_write_space(
                    session,
                    space_id=values["space_id"],
                    user_id=user_id,
                    user_info=user_info or {"id": str(user_id)},
                )
                if space is None:
                    raise TaskManagementError("Space not found", status_code=404)
                if not can_write:
                    raise TaskManagementError("Space access denied", status_code=403)
            updated = await ProjectRepository.update_project(
                session,
                project_id,
                commit=commit and ("name" not in values),
                **{
                    key: value
                    for key, value in values.items()
                    if value is not None or key == "space_id"
                },
            )
            if updated is not None and "name" in values:
                from ..services.project_information_docs import (
                    ensure_project_information_doc,
                    is_default_inbox_project,
                )
                if not is_default_inbox_project(updated) and not updated.is_completed:
                    await ensure_project_information_doc(
                        session,
                        project=updated,
                        user_id=user_id,
                    )
                if commit:
                    await session.commit()
                else:
                    await session.flush()
            return updated.to_dict() if updated else {"id": str(project_id)}
        space_id = values.get("space_id")
        if space_id is not None:
            from ..services.space_access import can_write_space

            can_write, space = await can_write_space(
                session,
                space_id=space_id,
                user_id=user_id,
                user_info=user_info or {"id": str(user_id)},
            )
            if space is None:
                raise TaskManagementError("Space not found", status_code=404)
            if not can_write:
                raise TaskManagementError("Space access denied", status_code=403)
        created = await ProjectRepository.create_project(
            session,
            owner_id=user_id,
            project_id=project_id,
            name=name,
            description=values["description"],
            aliases=values["aliases"],
            space_id=space_id,
            is_completed=bool(values.get("is_completed") or False),
            allow_join_requests=bool(
                values["allow_join_requests"]
                if values["allow_join_requests"] is not None
                else True
            ),
            storage_quota_mb=int(values["storage_quota_mb"] or 1000),
            project_metadata=values["project_metadata"] or {},
            commit=False,
        )
        # Sync create is another Project creation path. Seed the canonical
        # information node in the same transaction so a successful Project
        # response can never leave a missing/partially-created canonical root.
        from ..services.project_information_docs import (
            ensure_project_information_doc,
            is_default_inbox_project,
        )
        # Completed Projects retain any existing canonical rows for cleanup,
        # but must not bootstrap a fresh active identity during sync create.
        if not is_default_inbox_project(created) and not created.is_completed:
            await ensure_project_information_doc(
                session,
                project=created,
                user_id=user_id,
            )
        if commit:
            await session.commit()
        else:
            await session.flush()
        await session.refresh(created)
        return created.to_dict()

    if operation.action == "update":
        if existing is None or existing.deleted_at is not None:
            raise TaskManagementError("Project not found", status_code=404)
        await service.require_project_permission(
            session, project_id=project_id, user_id=user_id, permission="write"
        )
        _ensure_not_stale(existing.updated_at, operation.base_updated_at)
        update_values = {key: value for key, value in values.items() if value is not None}
        if "space_id" in values:
            # Explicit null moves a project back to the unclassified bucket;
            # a non-null target still has to pass the canonical Space policy.
            update_values["space_id"] = values["space_id"]
            if values["space_id"] is not None:
                from ..services.space_access import can_write_space

                can_write, space = await can_write_space(
                    session,
                    space_id=values["space_id"],
                    user_id=user_id,
                    user_info=user_info or {"id": str(user_id)},
                )
                if space is None:
                    raise TaskManagementError("Space not found", status_code=404)
                if not can_write:
                    raise TaskManagementError("Space access denied", status_code=403)
        if "name" in update_values and not update_values["name"]:
            update_values.pop("name")
        if (
            "name" in update_values
            and existing.is_completed
            and str(update_values["name"] or "").strip()
            != str(existing.name or "").strip()
        ):
            raise TaskManagementError(
                "完了済みProjectの名前は変更できません。再開してから変更してください",
                status_code=409,
            )
        updated = await ProjectRepository.update_project(
            session,
            project_id,
            commit=commit and ("name" not in update_values),
            **update_values,
        )
        if updated is not None and "name" in update_values:
            from ..services.project_information_docs import (
                ensure_project_information_doc,
                is_default_inbox_project,
            )
            if not is_default_inbox_project(updated) and not updated.is_completed:
                await ensure_project_information_doc(
                    session,
                    project=updated,
                    user_id=user_id,
                )
            if commit:
                await session.commit()
            else:
                await session.flush()
        return updated.to_dict() if updated else {"id": str(project_id)}

    raise TaskManagementError("Unsupported project sync action", status_code=400)


async def _apply_docs_push_operation(
    session: AsyncSession,
    *,
    user_id: UUID,
    operation: SyncOperation,
    workspace_root: str | os.PathLike[str] | None = None,
    commit: bool = True,
    after_commit: list[Callable[[], None]] | None = None,
    held_app_lock: UUID | None = None,
    outcome: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Docs push をRESTと同一の処理へ委譲し、サーバー版を検証する。"""
    if not commit and after_commit is None:
        raise ValueError("after_commit is required when commit=False")
    if operation.table == "knowledge_nodes" and "body_json" in operation.payload and operation.payload.get("body_json") is not None:
        try:
            operation.payload["body_json"] = normalize_docs_body_json(operation.payload["body_json"])
        except ValueError as exc:
            raise DocsOperationError(str(exc), status_code=400) from exc
    # app_readme の書き戻しはロックと実ファイル操作で同じ root を使う必要がある。
    # DocsGraphService に実効 root を持たせて docs_sync 側へ透過する。
    service = DocsGraphService(session, workspace_root=workspace_root)
    project_ref = operation.payload.get("project_id")
    # New clients send docs_library_id/docsLibraryId; old mobile sends
    # workspace_id/workspaceId.  Read all four explicitly at the wire boundary.
    workspace_ref = read_docs_library_id(operation.payload)
    library = None
    target_ref = (
        operation.payload.get("node_id")
        or operation.payload.get("parent_id")
        or str(operation.entity_id).split(":", 1)[0]
    )
    try:
        target_id = UUID(str(target_ref)) if target_ref else None
    except (TypeError, ValueError):
        target_id = None
    target = await session.get(KnowledgeNode, target_id) if target_id is not None else None
    if target is not None:
        # Existing node library is authoritative.  A stale project_id in an
        # update must not redirect the operation into a canonical library.
        library = await session.get(DocsLibrary, target.docs_library_id)
        target_readable = False
        if library is not None:
            target_readable = await library_can_read(session, library, user_id)
            if not target_readable:
                # Personal workspaces are shared at subtree roots, not at the
                # library row.  Check the target node ACL before returning a
                # generic not-found response for a legitimate shared edit.
                target_readable = await can_read_node(
                    session,
                    target,
                    user_id,
                    library=library,
                    include_archived=True,
                )
        if library is not None and not target_readable:
            raise DocsOperationError("Docs node not found", status_code=404)
    elif workspace_ref not in (None, ""):
        try:
            docs_library_id = UUID(str(workspace_ref))
        except (TypeError, ValueError) as exc:
            raise DocsOperationError("Invalid docs_library_id", status_code=400) from exc
        library = await session.get(DocsLibrary, docs_library_id)
        scope_readable = library is not None and await library_can_read(
            session, library, user_id
        )
        if not scope_readable and project_ref not in (None, ""):
            # A Project member's sync scope points at the owner's Personal
            # Library.  Library ownership is intentionally owner-only, so
            # validate the Project ACL/pointer as the authority instead of
            # rejecting a legitimate project create/update.
            try:
                scoped_project_id = UUID(str(project_ref))
                project_library = await service.get_project_information_library(
                    scoped_project_id,
                    user_id,
                )
            except (PermissionError, ValueError, TypeError):
                project_library = None
            scope_readable = bool(
                project_library is not None
                and library is not None
                and project_library.id == library.id
            )
        if library is None or not scope_readable:
            raise DocsOperationError("Docs library not found", status_code=404)
    elif project_ref not in (None, ""):
        try:
            project_id = UUID(str(project_ref))
        except (TypeError, ValueError) as exc:
            raise DocsOperationError("Invalid project_id", status_code=400) from exc
        try:
            library = await service.ensure_project_information_library(project_id, user_id)
        except PermissionError as exc:
            raise DocsOperationError(str(exc), status_code=403) from exc
        except ValueError as exc:
            raise DocsOperationError(str(exc), status_code=404) from exc
    if library is None:
        library = await service.ensure_library(user_id)
    return await apply_docs_operation(
        session,
        service,
        user_id=user_id,
        docs_library_id=library.id,
        table=operation.table,
        action=operation.action,
        entity_id=operation.entity_id,
        payload=operation.payload,
        base_updated_at=operation.base_updated_at,
        require_base_version=True,
        commit=commit,
        held_app_lock=held_app_lock,
        outcome=outcome,
    )


async def _dispatch_sync_operation(
    service,
    session,
    *,
    user_id,
    user_info,
    operation,
    workspace_root,
    commit: bool = True,
    after_commit: list[Callable[[], None]] | None = None,
    held_app_lock: UUID | None = None,
    outcome: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """push operation を table ごとの適用関数へ振り分ける。"""
    if operation.table == "projects":
        entity = await _apply_project_operation(
            service,
            session,
            user_id=user_id,
            user_info=user_info,
            operation=operation,
            workspace_root=workspace_root,
            commit=commit,
            after_commit=after_commit,
        )
    elif operation.table == "tasks":
        entity = await _apply_task_operation(
            service,
            session,
            user_id=user_id,
            operation=operation,
            commit=commit,
            after_commit=after_commit,
        )
    elif operation.table == "time_entries":
        entity = await _apply_time_entry_operation(
            service,
            session,
            user_id=user_id,
            operation=operation,
            commit=commit,
            after_commit=after_commit,
        )
    elif operation.table == "conversation_sessions":
        entity = await _apply_conversation_session_operation(
            session,
            user_id=user_id,
            user_info=user_info,
            operation=operation,
            commit=commit,
            after_commit=after_commit,
        )
    elif operation.table in DOCS_PUSH_TABLES:
        entity = await _apply_docs_push_operation(
            session,
            user_id=user_id,
            operation=operation,
            workspace_root=workspace_root,
            commit=commit,
            after_commit=after_commit,
            held_app_lock=held_app_lock,
            outcome=outcome,
        )
    else:
        raise TaskManagementError(
            f"Unsupported push table: {operation.table}",
            status_code=400,
        )
    return entity


def create_sync_router(
    get_db_manager,
    get_user_from_request,
    require_auth_dependency,
    workspace_root: str | os.PathLike[str] | None = None,
) -> APIRouter:
    """Mobile sync router。

    ``workspace_root`` は Project 削除のロック取得先と library 削除先、および
    Docs 経由の App README 書き戻し先を一致させるために透過する。``None`` なら
    ``AOITALK_WORKSPACES_DIR`` 由来の既定 root を使う。
    """
    router = APIRouter(prefix="/api/sync", tags=["sync"])
    service = TaskManagementService()

    async def _get_current_user(request: Request) -> tuple[UUID, dict[str, Any]]:
        user_info = await get_user_from_request(request)
        if not user_info or "id" not in user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return UUID(user_info["id"]), user_info

    @router.get(
        "/docs-diagnostic",
        response_model=SyncDocsDiagnosticResponse,
        response_model_exclude_none=True,
        responses={
            503: {
                "model": SyncDocsDiagnosticResponse,
                "description": "The diagnostic query could not be completed.",
            }
        },
    )
    async def docs_diagnostic(
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session: AsyncSession | None = None
        try:
            session = await get_db_manager().get_session()
            project_ids = await _accessible_project_ids(session, user_id)
            personal_result = await session.execute(
                select(DocsLibrary)
                .where(
                    DocsLibrary.owner_user_id == user_id,
                    DocsLibrary.library_type == "personal",
                )
                .limit(1)
            )
            personal_library = personal_result.scalars().first()
            normalization_result = await session.execute(
                select(DocsLibrary.id)
                .where(
                    DocsLibrary.owner_user_id == user_id,
                    DocsLibrary.library_type != "personal",
                )
                .limit(1)
            )
            needs_normalization = (
                normalization_result.scalar_one_or_none() is not None
            )
            scopes = await _docs_sync_scopes(
                session,
                user_id=user_id,
                project_ids=project_ids,
                personal_library=personal_library,
            )
            return SyncDocsDiagnosticResponse(
                status="ok",
                personal_library_exists=personal_library is not None,
                needs_normalization=needs_normalization,
                accessible_project_count=len(project_ids),
                accessible_scope_count=len(scopes),
            )
        except Exception as exc:  # noqa: BLE001
            exception_type = type(exc).__name__
            if not _SAFE_SYNC_EXCEPTION_TYPE_RE.fullmatch(exception_type):
                exception_type = "Exception"
            error_payload = SyncDocsDiagnosticResponse(
                status="error",
                error_code="diagnostic_query_failed",
                exception_type=exception_type,
            )
            return JSONResponse(
                status_code=503,
                content=jsonable_encoder(error_payload, exclude_none=True),
            )
        finally:
            if session is not None:
                try:
                    await session.close()
                except Exception:  # noqa: BLE001
                    # A cleanup failure must not expose driver exception text.
                    pass

    @router.get("/pull")
    async def pull(
        request: Request,
        since: Optional[str] = None,
        tables: Optional[str] = None,
        project_id: Optional[str] = None,
        docs_scope_id: Optional[str] = None,
        docs_pagination: bool = False,
        docs_scope_digest: Optional[str] = None,
        docs_snapshot_token: Optional[str] = None,
        docs_scope_revision: Optional[str] = None,
        docs_reconcile: bool = True,
        general_pagination: bool = False,
        general_cursors: Optional[str] = None,
        general_snapshot_token: Optional[str] = None,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        docs_digests = await _read_pull_docs_digests(request)
        docs_cursors = await _read_pull_docs_cursors(request)
        body_snapshot_token = await _read_pull_docs_snapshot_token(request)
        body_scope_revision = await _read_pull_docs_scope_revision(request)
        body_project_id = await _read_pull_project_id(request)
        body_docs_scope_id = await _read_pull_docs_scope_id(request)
        parsed_general_cursors = await _read_pull_general_cursors(request)
        parsed_general_digests = await _read_pull_general_digests(request)
        # Like docs_digests, this additive optional protocol is read directly
        # from Request so the existing generated endpoint signature stays intact.
        general_digest_version = request.query_params.get("general_digest_version", "0")
        if general_digest_version not in ("0", str(GENERAL_DIGEST_VERSION)):
            raise HTTPException(status_code=400, detail="Unsupported general sync digest version")
        requested_general_snapshot_token = (
            general_snapshot_token
            or await _read_pull_general_snapshot_token(request)
        )
        # Query parameters win, matching the existing digest/cursor readers;
        # GET bodies remain a test/tooling fallback for composite Docs scopes.
        project_id = project_id or body_project_id
        docs_scope_id = docs_scope_id or body_docs_scope_id
        # Query parameters win, matching the existing digest/cursor readers;
        # GET bodies remain a test/tooling fallback.
        docs_snapshot_token = _validate_docs_opaque_token(
            docs_snapshot_token or body_snapshot_token,
            "docs_snapshot_token",
        )
        requested_snapshot_token = docs_snapshot_token
        cursor_snapshot_token: Optional[str] = None
        docs_scope_revision = _validate_docs_opaque_token(
            docs_scope_revision or body_scope_revision,
            "docs_scope_revision",
        )
        requested = [
            table.strip()
            for table in (tables.split(",") if tables else sorted(SYNC_TABLES))
            if table.strip()
        ]
        unsupported = [table for table in requested if table not in SYNC_TABLES]
        if unsupported:
            raise HTTPException(status_code=400, detail=f"Unsupported tables: {unsupported}")
        unsupported_general = [
            table
            for table in parsed_general_cursors
            if table not in GENERAL_SYNC_PAGE_LIMITS
        ]
        if unsupported_general:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported general sync cursors: {unsupported_general}",
            )
        general_requested = general_pagination and any(
            table in GENERAL_SYNC_PAGE_LIMITS for table in requested
        )

        session = await get_db_manager().get_session()
        server_time = datetime.utcnow()
        try:
            since_dt = _parse_datetime(since, "since")
            general_digest_enabled = (
                general_requested
                and general_digest_version == str(GENERAL_DIGEST_VERSION)
                and since_dt is None
            )
            general_snapshot_at: Optional[datetime] = None
            general_snapshot_scope: Optional[str] = None
            # Application timestamps are assigned before the first database
            # write and therefore cannot prove commit ordering.  General v1
            # deliberately keeps the wall-clock page boundary only for
            # bounded traversal; completion never acknowledges it as an
            # incremental watermark.  Every completed run replays from
            # ``since=null`` on the next request, which is lossless even when
            # a writer obtains its XID after page 1 has advanced.
            general_snapshot_stable = False
            general_watermark: Optional[datetime] = None
            if general_requested:
                try:
                    snapshot_details = _decode_general_snapshot_details(
                        requested_general_snapshot_token
                    )
                except ValueError as exc:
                    raise HTTPException(
                        status_code=400, detail=str(exc)
                    ) from exc
                if snapshot_details is None:
                    general_snapshot_at = server_time
                else:
                    general_snapshot_at = snapshot_details.snapshot_at
                    general_snapshot_scope = snapshot_details.scope
                    # Never trust client-supplied stability metadata.  The
                    # current protocol intentionally has no safe incremental
                    # watermark; the token's timestamp is only a page bound.
                    general_snapshot_stable = False
                    general_watermark = None
                    # Continuation pages must echo the original snapshot
                    # boundary and replay watermark.
                    server_time = general_snapshot_at
                    general_snapshot_token = requested_general_snapshot_token
            await ProjectRepository.ensure_user_inbox_setup(session, user_id)
            await session.commit()
            project_ids = await _accessible_project_ids(session, user_id)
            if general_requested:
                current_general_scope = _general_scope_fingerprint(user_id, project_ids)
                if requested_general_snapshot_token:
                    # A general cursor is only meaningful for the exact ACL
                    # projection that produced it.  Recomputing the project
                    # set on every page prevents an access grant/revocation
                    # between pages from silently skipping rows before the
                    # final lastPulledAt watermark is committed.
                    if general_snapshot_scope != current_general_scope:
                        raise HTTPException(
                            status_code=409,
                            detail="General sync scope changed; restart pull",
                        )
                else:
                    if parsed_general_cursors:
                        raise HTTPException(
                            status_code=400,
                            detail="General sync cursor requires a snapshot token; restart pull",
                        )
                    general_snapshot_scope = current_general_scope
                    if general_snapshot_at is None:
                        general_snapshot_at = server_time
                    general_snapshot_token = _encode_general_snapshot_token(
                        general_snapshot_at,
                        general_snapshot_scope,
                        watermark=general_watermark,
                        stable=general_snapshot_stable,
                    )
            # Docs テーブルが要求された場合のみ library を解決する。
            docs_docs_library_id: Optional[UUID] = None
            docs_project_id: Optional[UUID] = None
            docs_scopes: list[dict[str, Any]] = []
            docs_revision_scopes: list[dict[str, Any]] = []
            docs_acl_entries: list[tuple[Any, Any]] = []
            if any(table in DOCS_SYNC_TABLES for table in requested):
                docs_service = DocsGraphService(session)
                if docs_scope_id:
                    try:
                        library = await session.get(
                            DocsLibrary, UUID(str(docs_scope_id))
                        )
                    except (TypeError, ValueError) as exc:
                        raise HTTPException(status_code=400, detail="Invalid Docs scope") from exc
                    if library is None:
                        raise HTTPException(status_code=404, detail="Docs scope not found")
                    if project_id:
                        # A composite scope identifies one Project inside the
                        # supplied library. Resolve through the SELECT-only
                        # canonical pointer validator so a stale/malformed
                        # Project.knowledge_node_id cannot expose a sibling
                        # Project sharing the same Personal library.
                        try:
                            docs_project_id = UUID(str(project_id))
                        except (TypeError, ValueError) as exc:
                            raise HTTPException(status_code=400, detail="Invalid project_id") from exc
                        project_row = await session.get(Project, docs_project_id)
                        if project_row is None or project_row.deleted_at is not None:
                            raise HTTPException(status_code=404, detail="Project not found")
                        try:
                            project_library = await docs_service.get_project_information_library(
                                docs_project_id,
                                user_id,
                            )
                        except PermissionError as exc:
                            if docs_snapshot_token or docs_cursors or docs_scope_revision:
                                raise HTTPException(
                                    status_code=409,
                                    detail="Docs sync scope revoked; restart pull",
                                ) from exc
                            raise HTTPException(status_code=403, detail=str(exc)) from exc
                        except (TypeError, ValueError) as exc:
                            raise HTTPException(status_code=404, detail="Project not found") from exc
                        if project_library is None:
                            if docs_snapshot_token or docs_cursors or docs_scope_revision:
                                raise HTTPException(
                                    status_code=409,
                                    detail="Docs sync scope revoked; restart pull",
                                )
                            raise HTTPException(
                                status_code=400,
                                detail="Project Docs pointer is invalid",
                            )
                        if project_library.id != library.id:
                            raise HTTPException(
                                status_code=400,
                                detail="Project Docs pointer does not match docs_scope_id",
                            )
                    else:
                        scope_readable = await library_can_read(
                            session, library, user_id
                        )
                        if not scope_readable:
                            # Personal libraries are shared by subtree ACL;
                            # there is intentionally no library-level share.
                            scope_readable = (
                                await session.scalar(
                                    select(KnowledgeNode.id)
                                    .where(
                                        docs_readable_node_predicate(
                                            KnowledgeNode,
                                            docs_library_id=library.id,
                                            user_id=user_id,
                                            library_owner_id=getattr(library, "owner_user_id", None),
                                        )
                                    )
                                    .limit(1)
                                )
                            ) is not None
                        if not scope_readable:
                            # A scoped continuation which lost access is a
                            # resumable authorization change, not a generic 403:
                            # the mobile promotion must quarantine the revoked
                            # scope and restart from its authoritative scope set.
                            if docs_snapshot_token or docs_cursors or docs_scope_revision:
                                raise HTTPException(
                                    status_code=409,
                                    detail="Docs sync scope revoked; restart pull",
                                )
                            raise HTTPException(status_code=403, detail="Docs scope access denied")
                elif project_id:
                    try:
                        docs_project_id = UUID(str(project_id))
                    except (TypeError, ValueError) as exc:
                        raise HTTPException(status_code=400, detail="Invalid project_id") from exc
                    project_row = await session.get(Project, docs_project_id)
                    if project_row is None or project_row.deleted_at is not None:
                        raise HTTPException(status_code=404, detail="Project not found")
                    try:
                        library = await docs_service.get_project_information_library(
                            docs_project_id, user_id
                        )
                    except PermissionError as exc:
                        if docs_snapshot_token or docs_cursors or docs_scope_revision:
                            raise HTTPException(
                                status_code=409,
                                detail="Docs sync scope revoked; restart pull",
                            ) from exc
                        raise HTTPException(status_code=403, detail=str(exc)) from exc
                    except (TypeError, ValueError) as exc:
                        raise HTTPException(status_code=404, detail="Project not found") from exc
                    if library is None:
                        if docs_snapshot_token or docs_cursors or docs_scope_revision:
                            raise HTTPException(
                                status_code=409,
                                detail="Docs sync scope revoked; restart pull",
                            )
                        raise HTTPException(
                            status_code=400,
                            detail="Project Docs pointer is invalid",
                        )
                else:
                    library = await docs_service.ensure_library(user_id)
                docs_docs_library_id = library.id
                personal_library = (
                    library
                    if library.owner_user_id == user_id
                    else await docs_service.ensure_library(user_id)
                )
                docs_scopes = await _docs_sync_scopes(
                    session,
                    user_id=user_id,
                    project_ids=project_ids,
                    personal_library=personal_library,
                )
                # The authoritative response advertises the complete active
                # set, but each cursor/revision must hash only the selected
                # composite scope.  Otherwise Project A ACL changes would
                # invalidate an independent Project B run in the same library.
                library_scope_id = str(docs_docs_library_id)
                if docs_project_id is not None:
                    docs_revision_scopes = [
                        scope
                        for scope in docs_scopes
                        if str(
                            scope.get("docs_library_id") or scope.get("workspace_id")
                        )
                        == library_scope_id
                        and str(scope.get("project_id")) == str(docs_project_id)
                    ]
                else:
                    # The root data slice is personal-only, but its
                    # authoritative scope revision still binds the complete
                    # active discovery set. A Project revoke must invalidate
                    # an in-flight root run so promotion can quarantine it.
                    docs_revision_scopes = docs_scopes
                if docs_pagination:
                    # Include the actor's canonical node-share rows in the
                    # scope revision. Aggregate docs_scopes alone cannot
                    # distinguish a child-level downgrade/revocation inside
                    # an otherwise still-active shared library. Restrict the
                    # projection to this composite scope so Project A ACL
                    # changes do not invalidate an independent Project B run.
                    try:
                        acl_stmt = (
                            select(
                                KnowledgeNodeShare.node_id,
                                KnowledgeNodeShare.permission,
                            )
                            .join(
                                KnowledgeNode,
                                KnowledgeNode.id == KnowledgeNodeShare.node_id,
                            )
                            .where(
                                KnowledgeNodeShare.user_id == user_id,
                                KnowledgeNode.docs_library_id == docs_docs_library_id,
                            )
                        )
                        if docs_project_id is not None:
                            acl_stmt = acl_stmt.where(
                                KnowledgeNode.project_id == docs_project_id
                            )
                        acl_rows = await session.execute(acl_stmt)
                        docs_acl_entries = list(acl_rows.all())
                    except Exception:  # noqa: BLE001
                        # A rolling deployment may not have the optional
                        # share table yet. The SQL-native pull ACL remains
                        # fail-closed; omit only the additive revision input.
                        docs_acl_entries = []
                # Materialize all library metadata before commit; the async
                # session may expire ORM attributes on commit.
                await session.commit()
            docs_scope_project_ids = (
                [docs_project_id] if docs_project_id is not None else []
            )
            current_docs_scope_digest = (
                calculate_docs_scope_digest(
                    docs_docs_library_id,
                    docs_scope_project_ids,
                    scope_project_id=docs_project_id,
                )
                if docs_pagination and docs_docs_library_id is not None
                else None
            )
            current_docs_scope_revision = (
                calculate_docs_scope_revision(
                    docs_docs_library_id,
                    docs_scope_project_ids,
                    docs_revision_scopes,
                    docs_acl_entries,
                    scope_project_id=docs_project_id,
                )
                if docs_pagination and docs_docs_library_id is not None
                else None
            )
            if docs_pagination and docs_docs_library_id is not None:
                # One opaque token identifies the root pull snapshot.  It is
                # echoed by every table page and is carried inside cursors;
                # no server-side mutable run state is required.
                has_docs_cursor = any(bool(value) for value in docs_cursors.values())
                cursor_tokens: set[str] = set()
                cursor_revisions: set[str] = set()
                if has_docs_cursor:
                    # Every resumable cursor issued by the additive protocol
                    # must carry its opaque token and ACL revision.  A
                    # malformed envelope or a pre-metadata cursor cannot be
                    # safely associated with this root snapshot, so fail
                    # closed instead of silently starting a new run.
                    for value in docs_cursors.values():
                        if not value:
                            continue
                        envelope = pull_cursor_envelope(value)
                        token = envelope.get("snapshot_token") if envelope else None
                        revision = envelope.get("scope_revision") if envelope else None
                        token = _validate_docs_opaque_token(token, "docs_snapshot_token")
                        revision = _validate_docs_opaque_token(
                            revision, "docs_scope_revision"
                        )
                        if token is None or revision is None:
                            raise HTTPException(
                                status_code=400,
                                detail="Invalid Docs sync cursor; restart pull",
                            )
                        cursor_tokens.add(token)
                        cursor_revisions.add(revision)
                if len(cursor_tokens) > 1:
                    raise HTTPException(
                        status_code=400,
                        detail="Docs sync snapshot changed; restart pull",
                    )
                if len(cursor_revisions) > 1:
                    raise HTTPException(
                        status_code=409,
                        detail="Docs sync scope changed; restart pull",
                    )
                cursor_token = next(iter(cursor_tokens), None)
                if (
                    requested_snapshot_token
                    and cursor_token
                    and requested_snapshot_token != cursor_token
                ):
                    raise HTTPException(
                        status_code=400,
                        detail="Docs sync snapshot changed; restart pull",
                    )
                # Preserve the token from a cursor when a legacy client does
                # not echo the new query field.  Cursors issued before the
                # additive token contract are rejected above rather than
                # silently restarting under a different snapshot.
                docs_snapshot_token = (
                    requested_snapshot_token
                    or cursor_token
                    or secrets.token_urlsafe(24)
                )
                cursor_snapshot_token = requested_snapshot_token or cursor_token
                if not has_docs_cursor:
                    cursor_snapshot_token = docs_snapshot_token
                if (
                    docs_scope_revision
                    and current_docs_scope_revision is not None
                    and docs_scope_revision != current_docs_scope_revision
                    and (has_docs_cursor or docs_snapshot_token)
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Docs sync scope changed; restart pull",
                    )
                if (
                    docs_scope_digest
                    and current_docs_scope_digest is not None
                    and docs_scope_digest != current_docs_scope_digest
                    and has_docs_cursor
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Docs sync scope changed; restart pull",
                    )
            else:
                current_docs_scope_revision = None
            # A v2 client may have a legacy last-pulled timestamp but no
            # additive ACL revision (and sometimes no per-table digest at all).
            # Since cannot safely describe ACL-visible historical rows, so the
            # first revision-less paginated pull must rebuild the visible set.
            # Once the server returns a revision, new clients echo it on
            # continuation and retain the normal since optimization.
            legacy_docs_without_revision = bool(
                docs_pagination and not docs_scope_revision
            )
            force_docs_full = bool(
                docs_pagination
                and current_docs_scope_digest is not None
                and docs_scope_digest is not None
                and docs_scope_digest != current_docs_scope_digest
            ) or legacy_docs_without_revision
            pulled = {}
            for table in requested:
                table_is_general = general_requested and table in GENERAL_SYNC_PAGE_LIMITS
                try:
                    if table_is_general and general_digest_enabled:
                        async def pull_general_page(cursor: Optional[str], table_name: str = table):
                            payload = await _pull_table(
                                table_name, session, user_id=user_id, project_ids=project_ids,
                                since=None, general_pagination=True,
                                general_cursor=cursor, general_snapshot_at=general_snapshot_at,
                            )
                            # Hash exactly FastAPI's wire representation, even
                            # if a serializer leaves a UUID/datetime/Decimal.
                            return jsonable_encoder(payload)

                        pulled[table] = await pull_general_digest_page(
                            table=table, scope=current_general_scope,
                            cursor=parsed_general_cursors.get(table),
                            client_digest=parsed_general_digests.get(table),
                            pull_page=pull_general_page,
                        )
                    else:
                        pulled[table] = await _pull_table(
                            table,
                            session,
                            user_id=user_id,
                            project_ids=project_ids,
                            since=since_dt,
                            docs_docs_library_id=docs_docs_library_id,
                            docs_digests=docs_digests,
                            docs_cursors=docs_cursors,
                            docs_pagination=docs_pagination,
                            force_docs_full=force_docs_full,
                            docs_reconcile=docs_reconcile,
                            docs_snapshot_token=cursor_snapshot_token,
                            docs_scope_revision=current_docs_scope_revision,
                            docs_project_id=docs_project_id,
                            docs_accessible_project_ids=docs_scope_project_ids,
                            general_pagination=table_is_general,
                            general_cursor=(
                                parsed_general_cursors.get(table)
                                if table_is_general
                                else None
                            ),
                            general_snapshot_at=(
                                general_snapshot_at if table_is_general else None
                            ),
                        )
                except (ValueError, TypeError) as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                if table not in DOCS_SYNC_TABLES and not table_is_general:
                    pulled[table]["cursor"] = server_time.isoformat()
            general_has_more = general_requested and any(
                table in GENERAL_SYNC_PAGE_LIMITS and bool(payload.get("cursor"))
                for table, payload in pulled.items()
            )
            docs_has_more = docs_pagination and any(
                table in DOCS_SYNC_TABLES and payload.get("cursor")
                for table, payload in pulled.items()
            )
            response = {
                "tables": pulled,
                "server_time": server_time.isoformat(),
                "has_more": bool(docs_has_more or general_has_more),
            }
            if docs_pagination:
                response["docs_pagination_version"] = DOCS_SYNC_PROTOCOL_VERSION
                response["docs_scope_digest"] = current_docs_scope_digest
                response["docs_snapshot_token"] = docs_snapshot_token
                response["docs_scope_revision"] = current_docs_scope_revision
            if general_requested:
                response["general_pagination_version"] = GENERAL_SYNC_PAGINATION_VERSION
                response["general_snapshot_token"] = general_snapshot_token
                if general_digest_enabled:
                    response["general_digest_version"] = GENERAL_DIGEST_VERSION
                # ``server_time`` remains the immutable page boundary.  The
                # replay watermark is separate: an unstable run (one that
                # observed an in-flight transaction before page 1) returns
                # null so Mobile deliberately replays from a full bootstrap
                # on the next run rather than risking a late-commit omission.
                response["general_watermark"] = _iso(general_watermark)
                response["general_has_more"] = bool(general_has_more)
            if docs_scopes:
                response["docs_scopes"] = docs_scopes
            return response
        finally:
            await session.close()

    @router.post("/push")
    async def push(
        payload: SyncPushPayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, user_info = await _get_current_user(request)
        session = await get_db_manager().get_session()
        results = []
        conversation_result_refs: list[tuple[dict[str, Any], dict[str, Any]]] = []
        try:
            for operation in payload.operations:
                try:
                    outcome: dict[str, Any] = {}
                    entity = await _dispatch_sync_operation(
                        service,
                        session,
                        user_id=user_id,
                        user_info=user_info,
                        operation=operation,
                        workspace_root=workspace_root,
                        outcome=outcome,
                    )
                    archive_rejected_status = outcome.get("archive_rejected_status")
                    if archive_rejected_status:
                        # README 更新は commit 済み。archive 部分だけが拒否された。
                        result_payload = {
                            "op_id": operation.op_id,
                            "status": "conflict"
                            if archive_rejected_status == 409
                            else "error",
                            "reason": "Docs README は更新済みのため、アーカイブのみ拒否されました",
                            "entity": entity,
                            "server_updated_at": entity.get("updated_at")
                            if isinstance(entity, dict)
                            else None,
                        }
                        if (
                            archive_rejected_status == 409
                            and "docs_business_conflict" in _SYNC_REASON_CODE_ALLOWLIST
                        ):
                            result_payload["reason_code"] = "docs_business_conflict"
                    else:
                        result_payload = {
                            "op_id": operation.op_id,
                            "status": "ok",
                            "entity": entity,
                            "server_updated_at": entity.get("updated_at")
                            if isinstance(entity, dict)
                            else None,
                        }
                    results.append(result_payload)
                    if operation.table == "conversation_sessions" and isinstance(entity, dict):
                        conversation_result_refs.append((result_payload, entity))
                except (TaskManagementError, DocsOperationError) as exc:
                    await session.rollback()
                    status = "conflict" if exc.status_code == 409 else "error"
                    conflict_entity: Optional[dict[str, Any]] = None
                    conflict_projection_failed = False
                    # Docs テーブルの entity_id は複合キー（"<node>:<field>" 等）が
                    # あり得るため、UUID として解釈できない場合は None として扱う
                    # （push 全体を 400 で落とさない）。
                    try:
                        entity_id = parse_uuid_or_400(operation.entity_id, "entity_id")
                    except HTTPException:
                        entity_id = None
                    try:
                        if status == "conflict" and operation.table in DOCS_PUSH_TABLES:
                            docs_workspace = await resolve_docs_conflict_library(
                                session,
                                user_id=user_id,
                                entity_id=operation.entity_id,
                                payload=operation.payload,
                            )
                            if docs_workspace is not None:
                                composite = operation.table in {
                                    "knowledge_node_supertags",
                                    "knowledge_field_values",
                                }
                                readable = (
                                    not composite
                                    or await docs_composite_response_readable(
                                        session,
                                        user_id=user_id,
                                        table=operation.table,
                                        entity_id=operation.entity_id,
                                        docs_library_id=docs_workspace.id,
                                    )
                                )
                                if readable:
                                    conflict_entity = await load_current_docs_entity(
                                        session,
                                        docs_library_id=docs_workspace.id,
                                        table=operation.table,
                                        entity_id=operation.entity_id,
                                        user_id=user_id,
                                    )
                                    # A versionless tombstone is only a failed
                                    # lookup, not an authoritative conflict snapshot.
                                    if (
                                        isinstance(conflict_entity, dict)
                                        and conflict_entity.get("deleted") is True
                                        and conflict_entity.get("updated_at") is None
                                    ):
                                        conflict_entity = None
                        elif status == "conflict" and entity_id is not None:
                            if operation.table == "tasks":
                                conflict_entity = await _load_current_task_payload(
                                    service, session, entity_id, user_id=user_id
                                )
                            elif operation.table == "time_entries":
                                conflict_entity = await _load_current_time_entry_payload(
                                    service, session, entity_id, user_id=user_id
                                )
                            elif operation.table == "conversation_sessions":
                                conflict_row = (
                                    await session.execute(
                                        select(ConversationSession).where(
                                            ConversationSession.id == entity_id
                                        )
                                    )
                                ).scalar_one_or_none()
                                if conflict_row is not None:
                                    await attach_private_root_metadata(session, [conflict_row])
                                    conflict_entity = _conversation_session_payload(conflict_row)
                    except Exception as projection_error:
                        # A secondary snapshot loader can fail after the
                        # typed operation rollback.  Clear SQLAlchemy's
                        # failed transaction state before the next operation.
                        await session.rollback()
                        conflict_projection_failed = True
                        safe_table = operation.table if operation.table in SYNC_TABLES else "unknown"
                        safe_action = operation.action if operation.action in {"create", "update", "delete", "archive", "restore"} else "unknown"
                        diagnostics = _safe_sync_exception_diagnostics(projection_error)
                        logger.warning(
                            "Sync conflict projection failed table=%s action=%s exception_type=%s sqlstate=%s source=%s",
                            safe_table,
                            safe_action,
                            diagnostics["exception_type"],
                            diagnostics["sqlstate"],
                            diagnostics["source_frame"],
                        )
                        conflict_entity = None
                    if conflict_projection_failed:
                        result_payload = {
                            "op_id": operation.op_id,
                            "status": "error",
                            "reason": "Conflict projection failed",
                            "reason_code": "sync_conflict_projection_failed",
                            "entity": None,
                            "server_updated_at": None,
                        }
                        results.append(result_payload)
                        continue
                    result_payload = {
                        "op_id": operation.op_id,
                        "status": status,
                        "reason": exc.message,
                        "entity": conflict_entity,
                        "server_updated_at": conflict_entity.get("updated_at")
                        if isinstance(conflict_entity, dict)
                        else None,
                    }
                    reason_code = getattr(exc, "reason_code", None)
                    if (
                        isinstance(reason_code, str)
                        and reason_code in _SYNC_REASON_CODE_ALLOWLIST
                    ):
                        result_payload["reason_code"] = reason_code
                    else:
                        # Preserve the existing domain detail.code contract.
                        # New exception attributes are accepted only from the
                        # finite wire-code set above.
                        error_detail = getattr(exc, "detail", None)
                        if isinstance(error_detail, dict) and error_detail.get("code"):
                            result_payload["reason_code"] = error_detail["code"]
                    results.append(result_payload)
                    if operation.table == "conversation_sessions" and isinstance(conflict_entity, dict):
                        conversation_result_refs.append((result_payload, conflict_entity))
                except Exception as exc:  # noqa: BLE001
                    await session.rollback()
                    safe_table = operation.table if operation.table in SYNC_TABLES else "unknown"
                    safe_action = operation.action if operation.action in {"create", "update", "delete", "archive", "restore"} else "unknown"
                    diagnostics = _safe_sync_exception_diagnostics(exc)
                    logger.error(
                        "Sync push operation failed table=%s action=%s exception_type=%s sqlstate=%s source=%s",
                        safe_table,
                        safe_action,
                        diagnostics["exception_type"],
                        diagnostics["sqlstate"],
                        diagnostics["source_frame"],
                    )
                    results.append(
                        {
                            "op_id": operation.op_id,
                            "status": "error",
                            "reason": "Sync operation failed",
                            "reason_code": "sync_operation_failed",
                        }
                    )
            if conversation_result_refs:
                conversation_entities = [
                    entity for _result, entity in conversation_result_refs
                ]
                await _apply_sync_conversation_statuses(
                    session,
                    conversation_entities,
                )
                for result_payload, entity in conversation_result_refs:
                    result_payload["server_updated_at"] = entity.get("updated_at")
            return {"results": results}
        finally:
            await session.close()

    return router

