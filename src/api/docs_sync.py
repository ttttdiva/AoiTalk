"""Docs 同期・書き込みの単一実装。

`sync_routes.py`（/api/sync/pull・push）と `docs_routes.py`（/api/docs/*）の両方から
import される。書き込みはすべて ``apply_docs_operation`` を通り、派生更新は
``DocsGraphService`` に集約される（不変条件 ``docs/docs_editing_invariants.md``）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import math
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

from sqlalchemy import and_, case, false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import (
    KnowledgeEdge,
    KnowledgeField,
    KnowledgeFieldValue,
    KnowledgeNode,
    KnowledgeNodePlacement,
    KnowledgeNodeSupertag,
    KnowledgeSupertag,
    KnowledgeSupertagField,
    DocsLibrary,
    Project,
    ProjectMember,
)
from ..memory.project_repository import ProjectRepository
from ..services.docs_graph_service import (
    DocsGraphService,
    is_explicit_blank_paragraph,
)
from ..services.docs_acl import (
    batch_sync_node_access,
    docs_readable_node_predicate,
    can_read_node,
    can_write_node,
    library_can_read,
    library_can_write,
)
from ..services.project_information_docs import is_default_inbox_project
from ..services.docs_library_compat import (
    read_docs_library_id,
    with_legacy_docs_library_aliases,
)
from ..utils.uuid_utils import parse_uuid

logger = logging.getLogger(__name__)

# pull 上限（sync_routes.SYNC_PULL_LIMITS からも参照）。
DOCS_PULL_LIMITS = {
    "knowledge_nodes": 5000,
    "knowledge_supertags": 5000,
    "knowledge_node_supertags": 10000,
    "knowledge_supertag_fields": 5000,
    "knowledge_fields": 5000,
    "knowledge_field_values": 10000,
    "knowledge_node_placements": 5000,
    "knowledge_edges": 5000,
}

DOCS_SYNC_TABLES = tuple(DOCS_PULL_LIMITS.keys())

# Web `normalizeDocsNodeType` と一致させる（page/block/object → node）。
_VALID_NODE_TYPES = {"node", "search", "day", "system"}

# ``body_json`` is a metadata envelope.  A user-visible multiline block is a
# first-class editable node whose payload uses this small, explicit shape.
# The old verbatim keys are intentionally rejected at every write boundary;
# existing rows are still serialized unchanged so the migration can read them.
_EDITABLE_DOC_BLOCK_TYPES = frozenset({"markdown", "code"})
_LEGACY_IMMUTABLE_BODY_KEYS = frozenset({"verbatim_blocks", "verbatim_content"})


# ---------------------------------------------------------------------------
# 共通ユーティリティ
# ---------------------------------------------------------------------------


def _iso(value: Optional[datetime | date]) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _docs_digest(items: list[str]) -> str:
    """権威セットのダイジェスト。項目文字列を sorted → "\\n".join → sha256 hex。

    空集合は空文字列（= sha256("")）を返す。モバイルが同じ規則で自分の SQLite
    キャッシュからダイジェストを計算し、pull 要求に添えて送ることで、サーバは
    「一致＝定常状態」を検出して権威セット全量の返却を省ける。
    """
    joined = "\n".join(sorted(items))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _effective_force_full(
    force_full: bool,
    client_digest: Optional[str],
    current_digest: str,
    *,
    user_id: UUID | None,
) -> bool:
    """Force a complete visible-set fetch when the client's digest changed.

    A Docs ACL grant/revoke can change the visible rows without touching their
    ``updated_at`` values.  Applying a ``since`` predicate after such a digest
    mismatch would omit newly visible historical rows, leaving the client with
    a saved digest that it never reconciled.  This safety bypass is only
    enabled for authenticated/ACL-aware pulls; direct legacy callers without
    ``user_id`` retain their historical since-only behavior.  A missing digest
    is the legacy incremental path; an explicitly mismatched digest must
    rebuild an authenticated scope.
    """

    return force_full or (
        user_id is not None
        and client_digest is not None
        and client_digest != current_digest
    )


def docs_scope_digest(
    docs_library_id: UUID,
    accessible_project_ids: list[UUID],
    scope_project_id: UUID | None = None,
) -> str:
    """Docs可視範囲のfingerprint。ページ間の権限変更検出にも使う。"""
    entries = [f"library:{docs_library_id}"]
    entries.extend(f"project:{project_id}" for project_id in accessible_project_ids)
    if scope_project_id is not None:
        entries.append(f"scope-project:{scope_project_id}")
    return _docs_digest(entries)


def docs_scope_revision(
    docs_library_id: UUID,
    accessible_project_ids: list[UUID],
    scopes: Optional[list[dict[str, Any]]] = None,
    acl_entries: Optional[list[tuple[Any, Any]]] = None,
    scope_project_id: UUID | None = None,
) -> str:
    """Return the canonical ACL revision for a Docs sync scope.

    ``docs_scope_digest`` is kept byte-for-byte compatible with the v2 mobile
    contract (library + project ids).  The additive revision also fingerprints
    the authoritative ``docs_scopes`` projection.  Consequently a project ACL
    change or a personal subtree share/revocation changes the revision even
    when the library/project ids remain unchanged.  ``acl_entries`` carries
    canonical node-share rows when available.  The projection is already
    produced by :func:`sync_routes._docs_sync_scopes`; callers that do not have
    it (legacy direct ``pull_docs_table`` users) safely fall back to the v2
    digest.
    """

    base = docs_scope_digest(
        docs_library_id,
        accessible_project_ids,
        scope_project_id=scope_project_id,
    )
    if not scopes and not acl_entries:
        return base
    entries = [f"base:{base}"]
    for scope in scopes or []:
        # Only fields which are part of the public authoritative ACL contract
        # participate.  Keep the encoding canonical so dict order cannot alter
        # a revision between requests.
        entries.append(
            "scope:"
            + json.dumps(
                {
                    "docs_library_id": scope.get("docs_library_id")
                    or scope.get("workspace_id"),
                    "project_id": scope.get("project_id"),
                    "source": scope.get("source"),
                    "access": scope.get("access"),
                    "read_only": bool(scope.get("read_only")),
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    for node_id, permission in acl_entries or []:
        entries.append(f"acl:{node_id}:{str(permission or '').lower()}")
    return _docs_digest(entries)


def _digest_dt(value: Optional[datetime]) -> str:
    """ダイジェスト用の updated_at 文字列。None は "none"。"""
    return value.isoformat() if value is not None else "none"


def _normalize_node_type(value: Any) -> str:
    text = str(value or "node")
    return text if text in _VALID_NODE_TYPES else "node"


def normalize_docs_body_json(value: Any) -> dict[str, Any]:
    """Validate the user-visible ``body_json`` wire contract.

    ``body_json`` may continue to carry non-content metadata (email,
    bookmark, provenance, and so on).  Typed markdown/code blocks are the
    only metadata envelope that carries visible multiline text and therefore
    require a string ``content`` and ``label``.  Legacy immutable content is
    rejected recursively so a caller cannot smuggle a read-only payload under
    a provenance object.  Reads deliberately do not call this helper: old
    rows remain readable until the explicit migration materializes them.
    """

    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("body_json must be an object")

    def reject_legacy_keys(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if str(key) in _LEGACY_IMMUTABLE_BODY_KEYS:
                    raise ValueError(
                        f"body_json.{key} is no longer accepted; use an editable markdown/code block"
                    )
                reject_legacy_keys(child)
        elif isinstance(item, list):
            for child in item:
                reject_legacy_keys(child)

    reject_legacy_keys(value)
    body = dict(value)
    block_type = body.get("block_type")
    if isinstance(block_type, str) and block_type in _EDITABLE_DOC_BLOCK_TYPES:
        if body.get("format") != "doc_block":
            raise ValueError("editable markdown/code blocks require format=doc_block")
        if not isinstance(body.get("content"), str):
            raise ValueError("editable markdown/code blocks require string content")
        if not isinstance(body.get("label"), str):
            raise ValueError("editable markdown/code blocks require string label")
    return body


def _node_pull_rank(row: KnowledgeNode) -> int:
    """Mobile一覧対象root→その他root→子孫の順序rank。"""
    if row.parent_id is not None:
        return 2
    if row.archived_at is None and _normalize_node_type(row.node_type) == "node":
        return 0
    return 1


def _parse_date(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _encode_pull_cursor(
    *parts: Any,
    scope_digest: str,
    snapshot_digest: str,
    snapshot_token: Optional[str] = None,
    scope_revision: Optional[str] = None,
) -> str:
    """Encode a stable keyset position as an opaque, self-validating cursor.

    The payload remains ``v=2`` for existing clients.  New clients receive
    the additive snapshot token/revision bindings; legacy cursors simply omit
    those keys and continue to validate against the original digest fields.
    """

    cursor_payload: dict[str, Any] = {
        "v": 2,
        "position": [
            _iso(part) if isinstance(part, (date, datetime)) else str(part)
            for part in parts
        ],
        "scope_digest": scope_digest,
        "snapshot_digest": snapshot_digest,
    }
    if snapshot_token:
        cursor_payload["snapshot_token"] = snapshot_token
    if scope_revision:
        cursor_payload["scope_revision"] = scope_revision
    payload = json.dumps(
        cursor_payload,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_pull_cursor(
    cursor: Optional[str],
    expected_parts: int,
    *,
    scope_digest: str,
    snapshot_digest: str,
    snapshot_token: Optional[str] = None,
    scope_revision: Optional[str] = None,
) -> Optional[list[str]]:
    if not cursor:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = base64.urlsafe_b64decode((cursor + padding).encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, TypeError, json.JSONDecodeError, binascii.Error) as exc:
        raise ValueError("Invalid Docs sync cursor") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("v") != 2
        or payload.get("scope_digest") != scope_digest
        or payload.get("snapshot_digest") != snapshot_digest
        or not isinstance(payload.get("position"), list)
        or len(payload["position"]) != expected_parts
        or not all(isinstance(part, str) for part in payload["position"])
    ):
        raise ValueError("Docs sync snapshot changed; restart pull")
    # New cursors bind to the opaque run snapshot and ACL revision.  A missing
    # key is accepted only for a legacy v2 cursor so old mobile builds remain
    # compatible; once a client opts into the additive metadata, mismatches
    # are explicit errors instead of silently mixing pages.
    if snapshot_token is not None and payload.get("snapshot_token") not in {
        None,
        snapshot_token,
    }:
        raise ValueError("Docs sync snapshot changed; restart pull")
    if scope_revision is not None and payload.get("scope_revision") not in {
        None,
        scope_revision,
    }:
        raise ValueError("Docs sync scope changed; restart pull")
    return payload["position"]


def pull_cursor_envelope(cursor: Optional[str]) -> Optional[dict[str, Any]]:
    """Decode a cursor envelope for route-level metadata validation.

    This is intentionally a non-authorizing convenience for the route: full
    cursor validation still happens in :func:`_decode_pull_cursor`.  It lets a
    client that predates ``docs_snapshot_token`` continue a cursor issued by a
    newer server without replacing the stable token on every request.
    """

    if not cursor:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = base64.urlsafe_b64decode((cursor + padding).encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, TypeError, json.JSONDecodeError, binascii.Error):
        return None
    return payload if isinstance(payload, dict) else None


def pull_cursor_snapshot_token(cursor: Optional[str]) -> Optional[str]:
    """Peek the additive snapshot token from a v2 cursor, if present."""

    payload = pull_cursor_envelope(cursor)
    token = payload.get("snapshot_token") if payload is not None else None
    return token if isinstance(token, str) and token else None


def _split_page(rows: list[Any], limit: int, cursor_factory) -> tuple[list[Any], Optional[str]]:
    page = rows[:limit]
    next_cursor = cursor_factory(page[-1]) if len(rows) > limit and page else None
    return page, next_cursor


def _annotate_docs_page(
    result: dict[str, Any],
    *,
    snapshot_revision: str,
    snapshot_token: Optional[str],
    scope_revision: str,
) -> dict[str, Any]:
    """Attach additive v2 page metadata without changing legacy keys."""

    result["docs_snapshot_revision"] = snapshot_revision
    result["docs_scope_revision"] = scope_revision
    if snapshot_token:
        result["docs_snapshot_token"] = snapshot_token
    return result


# ---------------------------------------------------------------------------
# シリアライザ（Web knowledge-docs-utils.ts の serialize* と同一キー・同一意味）
# ---------------------------------------------------------------------------


def serialize_docs_node(row: KnowledgeNode) -> dict[str, Any]:
    node_type = _normalize_node_type(row.node_type)
    query_json = row.query_json if isinstance(row.query_json, dict) else None
    payload = {
        "id": str(row.id),
        "docs_library_id": str(row.docs_library_id) if row.docs_library_id else None,
        "parent_id": str(row.parent_id) if row.parent_id else None,
        "root_page_id": str(row.root_page_id) if row.root_page_id else None,
        "project_id": str(row.project_id) if row.project_id else None,
        "system_key": row.system_key,
        "title": row.title,
        "aliases": row.aliases if isinstance(row.aliases, list) else [],
        "description": row.description or "",
        # ORM の暗号化プロパティが属性アクセス時に自動復号する（平文を返す）。
        "body_json": row.body_json if isinstance(row.body_json, dict) else {},
        "body_text": row.body_text or "",
        "node_type": node_type,
        "display_props": row.display_props if isinstance(row.display_props, dict) else {},
        "query_json": query_json if node_type == "search" else None,
        "view_json": row.view_json if isinstance(row.view_json, dict) else {},
        "day_date": _iso(row.day_date),
        "sort_order": float(row.sort_order or 0),
        "created_by": str(row.created_by) if row.created_by else None,
        "updated_by": str(row.updated_by) if row.updated_by else None,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "archived_at": _iso(row.archived_at),
    }
    return with_legacy_docs_library_aliases(payload, row.docs_library_id)


async def serialize_docs_node_for_sync(
    session: AsyncSession,
    row: KnowledgeNode,
    *,
    library: DocsLibrary,
    user_id: UUID,
    access_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach source/access metadata consumed by mobile's local ACL guard."""

    payload = serialize_docs_node(row)
    if access_metadata is not None:
        payload.update(
            {
                "source": access_metadata["source"],
                "access": access_metadata["access"],
                "read_only": bool(access_metadata["read_only"]),
            }
        )
        return payload
    if row.project_id is not None:
        source = "project"
        access = "write" if await can_write_node(
            session, row, user_id, library=library
        ) else "read"
    elif library.owner_user_id == user_id:
        source = "personal"
        access = "owner"
    else:
        source = "shared"
        access = "write" if await can_write_node(
            session, row, user_id, library=library
        ) else "read"
    payload.update(
        {
            "source": source,
            "access": access,
            "read_only": access == "read",
        }
    )
    return payload


def serialize_docs_supertag(row: KnowledgeSupertag) -> dict[str, Any]:
    payload = {
        "id": str(row.id),
        "docs_library_id": str(row.docs_library_id) if row.docs_library_id else None,
        "parent_supertag_id": str(row.parent_supertag_id) if row.parent_supertag_id else None,
        "system_key": row.system_key,
        "name": row.name,
        "base_type": row.base_type or "note",
        "description": row.description,
        "icon": row.icon,
        "color": row.color,
        "template_json": row.template_json if isinstance(row.template_json, dict) else {},
        "pinned_field_ids": row.pinned_field_ids if isinstance(row.pinned_field_ids, list) else [],
        "config_json": row.config_json if isinstance(row.config_json, dict) else {},
        "title_template": row.title_template,
        "ai_instructions": row.ai_instructions,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }
    return with_legacy_docs_library_aliases(payload, row.docs_library_id)


def serialize_docs_field(row: KnowledgeField) -> dict[str, Any]:
    payload = {
        "id": str(row.id),
        "docs_library_id": str(row.docs_library_id) if row.docs_library_id else None,
        "supertag_id": str(row.supertag_id) if row.supertag_id else None,
        "system_key": row.system_key,
        "name": row.name,
        "field_type": row.field_type or "text",
        "required": bool(row.required),
        "options_json": row.options_json if isinstance(row.options_json, dict) else {},
        "default_value_json": row.default_value_json,
        "sort_order": float(row.sort_order or 0),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }
    return with_legacy_docs_library_aliases(payload, row.docs_library_id)


def serialize_docs_supertag_field(row: KnowledgeSupertagField) -> dict[str, Any]:
    return {
        "supertag_id": str(row.supertag_id),
        "field_id": str(row.field_id),
        "sort_order": float(row.sort_order or 0),
        "required": bool(row.required),
        "show_in_template": bool(row.show_in_template),
        "optional": bool(row.optional),
        "created_at": _iso(row.created_at),
    }


def serialize_docs_node_supertag(row: KnowledgeNodeSupertag) -> dict[str, Any]:
    return {
        "node_id": str(row.node_id),
        "supertag_id": str(row.supertag_id),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "created_by": str(row.created_by) if row.created_by else None,
    }


def serialize_docs_field_value(row: KnowledgeFieldValue) -> dict[str, Any]:
    return {
        "node_id": str(row.node_id),
        "field_id": str(row.field_id),
        "value_json": row.value_json,
        "value_text": row.value_text,
        "value_number": row.value_number,
        "value_datetime": _iso(row.value_datetime),
        "target_node_id": str(row.target_node_id) if row.target_node_id else None,
        "updated_at": _iso(row.updated_at),
        "updated_by": str(row.updated_by) if row.updated_by else None,
    }


def serialize_docs_placement(row: KnowledgeNodePlacement) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "node_id": str(row.node_id),
        "parent_node_id": str(row.parent_node_id),
        "sort_order": float(row.sort_order or 0),
        "collapsed": bool(row.collapsed),
        "created_by": str(row.created_by) if row.created_by else None,
        "created_at": _iso(row.created_at),
    }


def serialize_docs_edge(row: KnowledgeEdge) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "source_node_id": str(row.source_node_id),
        "target_node_id": str(row.target_node_id),
        "relation_type": row.relation_type or "related_to",
        "confidence": float(row.confidence) if row.confidence is not None else 1.0,
        "created_by": str(row.created_by) if row.created_by else None,
        "created_at": _iso(row.created_at),
    }


# ---------------------------------------------------------------------------
# pull クエリ（library 単位・updated_at 差分 / 関連表はダイジェスト照合）
# ---------------------------------------------------------------------------


async def pull_docs_table(
    table: str,
    session: AsyncSession,
    *,
    docs_library_id: UUID | None = None,
    workspace_id: UUID | None = None,
    accessible_project_ids: list[UUID] | None = None,
    scope_project_id: UUID | None = None,
    since: Optional[datetime] = None,
    client_digest: Optional[str] = None,
    cursor: Optional[str] = None,
    paginate: bool = False,
    force_full: bool = False,
    include_authoritative_ids: bool = True,
    user_id: UUID | None = None,
    snapshot_token: Optional[str] = None,
    scope_revision: Optional[str] = None,
) -> dict[str, Any]:
    """Docs 8 テーブルの pull。sync_routes._pull_table から委譲される。

    Docs 8 テーブルは、毎回権威セット全量を返す代わりに
    サーバ計算のダイジェスト（``authoritative_digest``）を常に返す。モバイルが
    前回の全ページ適用後に保存した ``client_digest`` を送り、サーバの現在値と
    一致すれば「定常状態」とみなして権威セット（および行フェッチ）を省略する。
    不一致・未指定なら差分または全量 + authoritative_ids で reconcile
    する。ダイジェストは membership と可変列を織り込むため、行の追加・更新・削除で
    必ず変化する（可視範囲が変われば digest も変わる）。"""
    if docs_library_id is None:
        docs_library_id = workspace_id
    if docs_library_id is None:
        raise ValueError("docs_library_id is required")
    accessible_project_ids = accessible_project_ids or []
    limit = DOCS_PULL_LIMITS.get(table, 5000)
    workspace_row = await session.get(DocsLibrary, docs_library_id)
    if user_id is not None:
        # Compose the canonical SQL-native ACL predicate directly into each
        # table query.  ``visible_node_ids`` below remains a SELECT subquery,
        # never a Python list, so a 150k-node Personal Library cannot create a
        # giant IN predicate or bind-parameter payload.
        project_access = docs_readable_node_predicate(
            KnowledgeNode,
            docs_library_id=docs_library_id,
            user_id=user_id,
            library_owner_id=getattr(workspace_row, "owner_user_id", None),
        )
        if not accessible_project_ids:
            # The root scope is the actor's Personal/uncategorized Docs view.
            # Project nodes in the same Personal library are advertised as
            # separate composite scopes and must not be duplicated here.
            project_access = and_(project_access, KnowledgeNode.project_id.is_(None))
    else:
        project_access = (
            or_(
                KnowledgeNode.project_id.is_(None),
                KnowledgeNode.project_id.in_(accessible_project_ids),
            )
            if accessible_project_ids
            else KnowledgeNode.project_id.is_(None)
        )
    if scope_project_id is not None:
        # A project-scoped pull is a strict slice of a shared Personal library.
        # Keep the canonical ACL predicate, then add the identity discriminator
        # so sibling Projects cannot leak through the same library row.
        project_access = and_(
            project_access,
            KnowledgeNode.project_id == scope_project_id,
        )
    visible_node_ids = select(KnowledgeNode.id).where(
        KnowledgeNode.docs_library_id == docs_library_id,
        project_access,
    )
    # Workspace metadata is private unless the actor can see at least one
    # node carrying/referencing it.  Project members with library-wide ACL
    # naturally see all rows; personal node shares only see relevant metadata.
    visible_tag_ids = select(KnowledgeNodeSupertag.supertag_id).where(
        KnowledgeNodeSupertag.node_id.in_(visible_node_ids)
    )
    visible_field_ids = select(KnowledgeFieldValue.field_id).where(
        KnowledgeFieldValue.node_id.in_(visible_node_ids)
    )
    tag_access = (
        KnowledgeSupertag.id.in_(visible_tag_ids) if user_id is not None else True
    )
    field_access = (
        or_(
            KnowledgeField.supertag_id.in_(visible_tag_ids),
            KnowledgeField.id.in_(visible_field_ids),
        )
        if user_id is not None
        else True
    )
    supertag_field_access = (
        KnowledgeSupertagField.supertag_id.in_(visible_tag_ids)
        if user_id is not None
        else True
    )
    node_supertag_access = (
        and_(
            KnowledgeSupertag.docs_library_id == docs_library_id,
            KnowledgeNodeSupertag.supertag_id.in_(visible_tag_ids),
        )
        if user_id is not None
        else True
    )
    scope_fingerprint = docs_scope_digest(
        docs_library_id,
        accessible_project_ids,
        scope_project_id=scope_project_id,
    )
    # ``scope_revision`` is the canonical ACL fingerprint calculated by the
    # route from the authoritative docs_scopes set.  Direct legacy callers do
    # not provide it and retain the v2 digest-only cursor contract.
    effective_scope_revision = scope_revision or scope_fingerprint

    if table == "knowledge_nodes":
        digest_rows = (
            await session.execute(
                select(KnowledgeNode.id, KnowledgeNode.updated_at).where(
                    KnowledgeNode.docs_library_id == docs_library_id,
                    project_access,
                )
            )
        ).all()
        digest = _docs_digest(
            [f"{id_}:{_digest_dt(updated_at)}" for id_, updated_at in digest_rows]
        )
        effective_force_full = _effective_force_full(
            force_full,
            client_digest,
            digest,
            user_id=user_id,
        )
        stmt = select(KnowledgeNode).where(
            KnowledgeNode.docs_library_id == docs_library_id,
            project_access,
        )
        # 大規模workspaceの再同期中でもページ一覧を先に復元できるよう、
        # mobile一覧対象root→その他root→子孫の順に送る。cursorにも同じrankを
        # 含めて全ページを重複・欠落なく走査する。
        mobile_node_type = or_(
            KnowledgeNode.node_type.is_(None),
            KnowledgeNode.node_type.notin_(("search", "day", "system")),
        )
        visible_root = and_(
            KnowledgeNode.parent_id.is_(None),
            KnowledgeNode.archived_at.is_(None),
            mobile_node_type,
        )
        root_rank = case(
            (visible_root, 0),
            (KnowledgeNode.parent_id.is_(None), 1),
            else_=2,
        )
        if since and not effective_force_full:
            stmt = stmt.where(
                or_(KnowledgeNode.updated_at > since, KnowledgeNode.archived_at > since)
            )
        if paginate:
            cursor_parts = _decode_pull_cursor(
                cursor,
                3,
                scope_digest=scope_fingerprint,
                snapshot_digest=digest,
                snapshot_token=snapshot_token,
                scope_revision=effective_scope_revision,
            )
            if cursor_parts:
                cursor_root_rank = int(cursor_parts[0])
                cursor_updated_at = datetime.fromisoformat(cursor_parts[1])
                cursor_id = UUID(cursor_parts[2])
                stmt = stmt.where(
                    or_(
                        root_rank > cursor_root_rank,
                        and_(
                            root_rank == cursor_root_rank,
                            or_(
                                KnowledgeNode.updated_at > cursor_updated_at,
                                and_(
                                    KnowledgeNode.updated_at == cursor_updated_at,
                                    KnowledgeNode.id > cursor_id,
                                ),
                            ),
                        ),
                    )
                )
            stmt = stmt.order_by(
                root_rank.asc(), KnowledgeNode.updated_at.asc(), KnowledgeNode.id.asc()
            ).limit(limit + 1)
        else:
            stmt = stmt.order_by(KnowledgeNode.updated_at.desc()).limit(limit)
        rows = list((await session.execute(stmt)).scalars().all())
        next_cursor = None
        if paginate:
            rows, next_cursor = _split_page(
                rows,
                limit,
                lambda row: _encode_pull_cursor(
                    _node_pull_rank(row),
                    row.updated_at,
                    row.id,
                    scope_digest=scope_fingerprint,
                    snapshot_digest=digest,
                    snapshot_token=snapshot_token,
                    scope_revision=effective_scope_revision,
                ),
            )
        sync_workspace = workspace_row or await session.get(
            DocsLibrary, docs_library_id
        )
        serialized_nodes = [serialize_docs_node(row) for row in rows]
        if user_id is not None and sync_workspace is not None:
            access_by_node = await batch_sync_node_access(
                session,
                rows,
                library=sync_workspace,
                user_id=user_id,
            )
            serialized_nodes = [
                await serialize_docs_node_for_sync(
                    session,
                    row,
                    library=sync_workspace,
                    user_id=user_id,
                    access_metadata=access_by_node.get(row.id),
                )
                for row in rows
            ]
        result: dict[str, Any] = {
            # アーカイブは archived_at 付き通常行として changes で返す（tombstone にしない）。
            "changes": serialized_nodes,
            "tombstones": [],
            "cursor": next_cursor,
            "authoritative_count": len(digest_rows),
        }
        if not effective_force_full and client_digest is not None and client_digest == digest:
            # 権威状態一致。changes は since 差分のまま返すが、
            # 権威 ID 集合の全量返却は省く（定常時の応答をほぼ空にする）。
            if next_cursor is None:
                result["authoritative_digest"] = digest
            return _annotate_docs_page(
                result,
                snapshot_revision=digest,
                snapshot_token=snapshot_token,
                scope_revision=effective_scope_revision,
            )
        # 不一致・未指定: ハード削除を reconcile できるよう権威 ID 集合を全量返す。
        if next_cursor is None:
            if include_authoritative_ids:
                result["authoritative_ids"] = [
                    str(id_) for id_, _ in digest_rows
                ]
            result["authoritative_scope_id"] = str(docs_library_id)
            result["authoritative_digest"] = digest
        return _annotate_docs_page(
            result,
            snapshot_revision=digest,
            snapshot_token=snapshot_token,
            scope_revision=effective_scope_revision,
        )

    if table == "knowledge_supertags":
        digest_rows = (
            await session.execute(
                select(KnowledgeSupertag.id, KnowledgeSupertag.updated_at).where(
                    KnowledgeSupertag.docs_library_id == docs_library_id,
                    tag_access,
                )
            )
        ).all()
        digest = _docs_digest(
            [f"{id_}:{_digest_dt(updated_at)}" for id_, updated_at in digest_rows]
        )
        effective_force_full = _effective_force_full(
            force_full,
            client_digest,
            digest,
            user_id=user_id,
        )
        if not effective_force_full and client_digest is not None and client_digest == digest:
            return _annotate_docs_page(
                {
                    "changes": [],
                    "tombstones": [],
                    "cursor": None,
                    "authoritative_count": len(digest_rows),
                    "authoritative_digest": digest,
                    },
                snapshot_revision=digest,
                snapshot_token=snapshot_token,
                scope_revision=effective_scope_revision,
            )
        stmt = select(KnowledgeSupertag).where(
            KnowledgeSupertag.docs_library_id == docs_library_id,
            tag_access,
        )
        if since and not effective_force_full:
            stmt = stmt.where(KnowledgeSupertag.updated_at > since)
        if paginate:
            cursor_parts = _decode_pull_cursor(
                cursor,
                2,
                scope_digest=scope_fingerprint,
                snapshot_digest=digest,
                snapshot_token=snapshot_token,
                scope_revision=effective_scope_revision,
            )
            if cursor_parts:
                cursor_updated_at = datetime.fromisoformat(cursor_parts[0])
                cursor_id = UUID(cursor_parts[1])
                stmt = stmt.where(
                    or_(
                        KnowledgeSupertag.updated_at > cursor_updated_at,
                        and_(
                            KnowledgeSupertag.updated_at == cursor_updated_at,
                            KnowledgeSupertag.id > cursor_id,
                        ),
                    )
                )
            stmt = stmt.order_by(
                KnowledgeSupertag.updated_at.asc(), KnowledgeSupertag.id.asc()
            ).limit(limit + 1)
        else:
            stmt = stmt.order_by(KnowledgeSupertag.updated_at.desc()).limit(limit)
        rows = list((await session.execute(stmt)).scalars().all())
        next_cursor = None
        if paginate:
            rows, next_cursor = _split_page(
                rows,
                limit,
                lambda row: _encode_pull_cursor(
                    row.updated_at,
                    row.id,
                    scope_digest=scope_fingerprint,
                    snapshot_digest=digest,
                    snapshot_token=snapshot_token,
                    scope_revision=effective_scope_revision,
                ),
            )
        result = {
            "changes": [serialize_docs_supertag(row) for row in rows],
            "tombstones": [],
            "cursor": next_cursor,
            "authoritative_count": len(digest_rows),
        }
        if next_cursor is None:
            if include_authoritative_ids:
                result["authoritative_ids"] = [str(id_) for id_, _ in digest_rows]
            result["authoritative_scope_id"] = str(docs_library_id)
            result["authoritative_digest"] = digest
        return _annotate_docs_page(
            result,
            snapshot_revision=digest,
            snapshot_token=snapshot_token,
            scope_revision=effective_scope_revision,
        )

    if table == "knowledge_fields":
        digest_rows = (
            await session.execute(
                select(KnowledgeField.id, KnowledgeField.updated_at).where(
                    KnowledgeField.docs_library_id == docs_library_id,
                    field_access,
                )
            )
        ).all()
        digest = _docs_digest(
            [f"{id_}:{_digest_dt(updated_at)}" for id_, updated_at in digest_rows]
        )
        effective_force_full = _effective_force_full(
            force_full,
            client_digest,
            digest,
            user_id=user_id,
        )
        if not effective_force_full and client_digest is not None and client_digest == digest:
            return _annotate_docs_page(
                {
                    "changes": [],
                    "tombstones": [],
                    "cursor": None,
                    "authoritative_count": len(digest_rows),
                    "authoritative_digest": digest,
                    },
                snapshot_revision=digest,
                snapshot_token=snapshot_token,
                scope_revision=effective_scope_revision,
            )
        stmt = select(KnowledgeField).where(
            KnowledgeField.docs_library_id == docs_library_id,
            field_access,
        )
        if since and not effective_force_full:
            stmt = stmt.where(KnowledgeField.updated_at > since)
        if paginate:
            cursor_parts = _decode_pull_cursor(
                cursor,
                2,
                scope_digest=scope_fingerprint,
                snapshot_digest=digest,
                snapshot_token=snapshot_token,
                scope_revision=effective_scope_revision,
            )
            if cursor_parts:
                cursor_updated_at = datetime.fromisoformat(cursor_parts[0])
                cursor_id = UUID(cursor_parts[1])
                stmt = stmt.where(
                    or_(
                        KnowledgeField.updated_at > cursor_updated_at,
                        and_(
                            KnowledgeField.updated_at == cursor_updated_at,
                            KnowledgeField.id > cursor_id,
                        ),
                    )
                )
            stmt = stmt.order_by(
                KnowledgeField.updated_at.asc(), KnowledgeField.id.asc()
            ).limit(limit + 1)
        else:
            stmt = stmt.order_by(KnowledgeField.updated_at.desc()).limit(limit)
        rows = list((await session.execute(stmt)).scalars().all())
        next_cursor = None
        if paginate:
            rows, next_cursor = _split_page(
                rows,
                limit,
                lambda row: _encode_pull_cursor(
                    row.updated_at,
                    row.id,
                    scope_digest=scope_fingerprint,
                    snapshot_digest=digest,
                    snapshot_token=snapshot_token,
                    scope_revision=effective_scope_revision,
                ),
            )
        result = {
            "changes": [serialize_docs_field(row) for row in rows],
            "tombstones": [],
            "cursor": next_cursor,
            "authoritative_count": len(digest_rows),
        }
        if next_cursor is None:
            if include_authoritative_ids:
                result["authoritative_ids"] = [str(id_) for id_, _ in digest_rows]
            result["authoritative_scope_id"] = str(docs_library_id)
            result["authoritative_digest"] = digest
        return _annotate_docs_page(
            result,
            snapshot_revision=digest,
            snapshot_token=snapshot_token,
            scope_revision=effective_scope_revision,
        )

    if table == "knowledge_field_values":
        # value クリア（行削除）が Web/他クライアントで起きても伝播するよう、削除を
        # 織り込んだダイジェストで照合する。ダイジェストは (node_id, field_id,
        # updated_at) から計算するので、値の更新（updated_at 変化）も削除も検知できる。
        digest_rows = (
            await session.execute(
                select(
                    KnowledgeFieldValue.node_id,
                    KnowledgeFieldValue.field_id,
                    KnowledgeFieldValue.updated_at,
                )
                .join(KnowledgeNode, KnowledgeNode.id == KnowledgeFieldValue.node_id)
                .join(KnowledgeField, KnowledgeField.id == KnowledgeFieldValue.field_id)
                .where(
                    KnowledgeFieldValue.node_id.in_(visible_node_ids),
                    KnowledgeField.docs_library_id == docs_library_id,
                    (
                        KnowledgeFieldValue.target_node_id.is_(None)
                        | KnowledgeFieldValue.target_node_id.in_(visible_node_ids)
                    )
                    if user_id is not None
                    else True,
                )
            )
        ).all()
        digest = _docs_digest(
            [
                f"{node_id}:{field_id}:{_digest_dt(updated_at)}"
                for node_id, field_id, updated_at in digest_rows
            ]
        )
        effective_force_full = _effective_force_full(
            force_full,
            client_digest,
            digest,
            user_id=user_id,
        )
        if not effective_force_full and client_digest is not None and client_digest == digest:
            # 一致＝定常状態。行フェッチも authoritative_ids も省く。
            return _annotate_docs_page(
                {
                    "changes": [],
                    "tombstones": [],
                    "cursor": None,
                    "authoritative_count": len(digest_rows),
                    "authoritative_digest": digest,
                    },
                snapshot_revision=digest,
                snapshot_token=snapshot_token,
                scope_revision=effective_scope_revision,
            )
        # 不一致・未指定: since 差分の changes + 権威 ID 集合全量で reconcile する。
        stmt = (
            select(KnowledgeFieldValue)
            .join(KnowledgeNode, KnowledgeNode.id == KnowledgeFieldValue.node_id)
            .join(KnowledgeField, KnowledgeField.id == KnowledgeFieldValue.field_id)
            .where(
                KnowledgeFieldValue.node_id.in_(visible_node_ids),
                KnowledgeField.docs_library_id == docs_library_id,
                (
                    KnowledgeFieldValue.target_node_id.is_(None)
                    | KnowledgeFieldValue.target_node_id.in_(visible_node_ids)
                )
                if user_id is not None
                else True,
            )
        )
        if since and not effective_force_full:
            stmt = stmt.where(KnowledgeFieldValue.updated_at > since)
        if paginate:
            cursor_parts = _decode_pull_cursor(
                cursor,
                3,
                scope_digest=scope_fingerprint,
                snapshot_digest=digest,
                snapshot_token=snapshot_token,
                scope_revision=effective_scope_revision,
            )
            if cursor_parts:
                cursor_updated_at = datetime.fromisoformat(cursor_parts[0])
                cursor_node_id = UUID(cursor_parts[1])
                cursor_field_id = UUID(cursor_parts[2])
                stmt = stmt.where(
                    or_(
                        KnowledgeFieldValue.updated_at > cursor_updated_at,
                        and_(
                            KnowledgeFieldValue.updated_at == cursor_updated_at,
                            or_(
                                KnowledgeFieldValue.node_id > cursor_node_id,
                                and_(
                                    KnowledgeFieldValue.node_id == cursor_node_id,
                                    KnowledgeFieldValue.field_id > cursor_field_id,
                                ),
                            ),
                        ),
                    )
                )
            stmt = stmt.order_by(
                KnowledgeFieldValue.updated_at.asc(),
                KnowledgeFieldValue.node_id.asc(),
                KnowledgeFieldValue.field_id.asc(),
            ).limit(limit + 1)
        else:
            stmt = stmt.limit(limit)
        rows = list((await session.execute(stmt)).scalars().all())
        next_cursor = None
        if paginate:
            rows, next_cursor = _split_page(
                rows,
                limit,
                lambda row: _encode_pull_cursor(
                    row.updated_at,
                    row.node_id,
                    row.field_id,
                    scope_digest=scope_fingerprint,
                    snapshot_digest=digest,
                    snapshot_token=snapshot_token,
                    scope_revision=effective_scope_revision,
                ),
            )
        result = {
            "changes": [serialize_docs_field_value(row) for row in rows],
            "tombstones": [],
            "cursor": next_cursor,
            "authoritative_count": len(digest_rows),
        }
        if next_cursor is None:
            if include_authoritative_ids:
                result["authoritative_ids"] = [
                    f"{node_id}:{field_id}" for node_id, field_id, _ in digest_rows
                ]
            result["authoritative_digest"] = digest
        return _annotate_docs_page(
            result,
            snapshot_revision=digest,
            snapshot_token=snapshot_token,
            scope_revision=effective_scope_revision,
        )

    # 関連表も削除を伝播できるよう、権威セットのダイジェストで照合する。
    if table == "knowledge_node_supertags":
        digest_rows = (
            await session.execute(
                select(
                    KnowledgeNodeSupertag.node_id,
                    KnowledgeNodeSupertag.supertag_id,
                    KnowledgeNodeSupertag.updated_at,
                )
                .join(KnowledgeNode, KnowledgeNode.id == KnowledgeNodeSupertag.node_id)
                .join(
                    KnowledgeSupertag,
                    KnowledgeSupertag.id == KnowledgeNodeSupertag.supertag_id,
                )
                .where(
                    KnowledgeNodeSupertag.node_id.in_(visible_node_ids),
                    node_supertag_access,
                )
            )
        ).all()
        digest = _docs_digest(
            [
                f"{node_id}:{supertag_id}:{_digest_dt(updated_at)}"
                for node_id, supertag_id, updated_at in digest_rows
            ]
        )
        effective_force_full = _effective_force_full(
            force_full,
            client_digest,
            digest,
            user_id=user_id,
        )
        if not effective_force_full and client_digest is not None and client_digest == digest:
            return _annotate_docs_page(
                {
                    "changes": [],
                    "tombstones": [],
                    "cursor": None,
                    "authoritative_count": len(digest_rows),
                    "authoritative_digest": digest,
                    },
                snapshot_revision=digest,
                snapshot_token=snapshot_token,
                scope_revision=effective_scope_revision,
            )
        stmt = (
            select(KnowledgeNodeSupertag)
            .join(KnowledgeNode, KnowledgeNode.id == KnowledgeNodeSupertag.node_id)
            .join(
                KnowledgeSupertag,
                KnowledgeSupertag.id == KnowledgeNodeSupertag.supertag_id,
            )
            .where(
                KnowledgeNodeSupertag.node_id.in_(visible_node_ids),
                node_supertag_access,
            )
        )
        if since and not effective_force_full:
            stmt = stmt.where(KnowledgeNodeSupertag.updated_at > since)
        if paginate:
            cursor_parts = _decode_pull_cursor(
                cursor,
                3,
                scope_digest=scope_fingerprint,
                snapshot_digest=digest,
                snapshot_token=snapshot_token,
                scope_revision=effective_scope_revision,
            )
            if cursor_parts:
                cursor_updated_at = datetime.fromisoformat(cursor_parts[0])
                cursor_node_id = UUID(cursor_parts[1])
                cursor_supertag_id = UUID(cursor_parts[2])
                stmt = stmt.where(
                    or_(
                        KnowledgeNodeSupertag.updated_at > cursor_updated_at,
                        and_(
                            KnowledgeNodeSupertag.updated_at == cursor_updated_at,
                            or_(
                                KnowledgeNodeSupertag.node_id > cursor_node_id,
                                and_(
                                    KnowledgeNodeSupertag.node_id == cursor_node_id,
                                    KnowledgeNodeSupertag.supertag_id > cursor_supertag_id,
                                ),
                            ),
                        ),
                    )
                )
            stmt = stmt.order_by(
                KnowledgeNodeSupertag.updated_at.asc(),
                KnowledgeNodeSupertag.node_id.asc(),
                KnowledgeNodeSupertag.supertag_id.asc(),
            ).limit(limit + 1)
        else:
            stmt = stmt.limit(limit)
        rows = list((await session.execute(stmt)).scalars().all())
        next_cursor = None
        if paginate:
            rows, next_cursor = _split_page(
                rows,
                limit,
                lambda row: _encode_pull_cursor(
                    row.updated_at,
                    row.node_id,
                    row.supertag_id,
                    scope_digest=scope_fingerprint,
                    snapshot_digest=digest,
                    snapshot_token=snapshot_token,
                    scope_revision=effective_scope_revision,
                ),
            )
        result = {
            "changes": [serialize_docs_node_supertag(row) for row in rows],
            "tombstones": [],
            "cursor": next_cursor,
            "authoritative_count": len(digest_rows),
        }
        if next_cursor is None:
            if include_authoritative_ids:
                result["authoritative_ids"] = [
                    f"{node_id}:{supertag_id}"
                    for node_id, supertag_id, _ in digest_rows
                ]
            result["authoritative_digest"] = digest
        return _annotate_docs_page(
            result,
            snapshot_revision=digest,
            snapshot_token=snapshot_token,
            scope_revision=effective_scope_revision,
        )

    if table == "knowledge_supertag_fields":
        # updated_at が無いため、可変列（sort_order/required/show_in_template/optional）
        # をダイジェストへ織り込む。列変更・削除いずれも digest が変化する。
        digest_rows = (
            await session.execute(
                select(
                    KnowledgeSupertagField.supertag_id,
                    KnowledgeSupertagField.field_id,
                    KnowledgeSupertagField.sort_order,
                    KnowledgeSupertagField.required,
                    KnowledgeSupertagField.show_in_template,
                    KnowledgeSupertagField.optional,
                )
                .join(
                    KnowledgeSupertag,
                    KnowledgeSupertag.id == KnowledgeSupertagField.supertag_id,
                )
                .join(
                    KnowledgeField,
                    KnowledgeField.id == KnowledgeSupertagField.field_id,
                )
                .where(
                    KnowledgeSupertag.docs_library_id == docs_library_id,
                    KnowledgeField.docs_library_id == docs_library_id,
                    supertag_field_access,
                )
            )
        ).all()
        digest = _docs_digest(
            [
                f"{supertag_id}:{field_id}:{sort_order!r}:{int(required)}:"
                f"{int(show_in_template)}:{int(optional)}"
                for (
                    supertag_id,
                    field_id,
                    sort_order,
                    required,
                    show_in_template,
                    optional,
                ) in digest_rows
            ]
        )
        effective_force_full = _effective_force_full(
            force_full,
            client_digest,
            digest,
            user_id=user_id,
        )
        if not effective_force_full and client_digest is not None and client_digest == digest:
            return _annotate_docs_page(
                {
                    "changes": [],
                    "tombstones": [],
                    "cursor": None,
                    "authoritative_count": len(digest_rows),
                    "authoritative_digest": digest,
                    },
                snapshot_revision=digest,
                snapshot_token=snapshot_token,
                scope_revision=effective_scope_revision,
            )
        stmt = (
            select(KnowledgeSupertagField)
            .join(KnowledgeSupertag, KnowledgeSupertag.id == KnowledgeSupertagField.supertag_id)
            .join(KnowledgeField, KnowledgeField.id == KnowledgeSupertagField.field_id)
            .where(
                KnowledgeSupertag.docs_library_id == docs_library_id,
                KnowledgeField.docs_library_id == docs_library_id,
                supertag_field_access,
            )
        )
        if paginate:
            cursor_parts = _decode_pull_cursor(
                cursor,
                2,
                scope_digest=scope_fingerprint,
                snapshot_digest=digest,
                snapshot_token=snapshot_token,
                scope_revision=effective_scope_revision,
            )
            if cursor_parts:
                cursor_supertag_id = UUID(cursor_parts[0])
                cursor_field_id = UUID(cursor_parts[1])
                stmt = stmt.where(
                    or_(
                        KnowledgeSupertagField.supertag_id > cursor_supertag_id,
                        and_(
                            KnowledgeSupertagField.supertag_id == cursor_supertag_id,
                            KnowledgeSupertagField.field_id > cursor_field_id,
                        ),
                    )
                )
            stmt = stmt.order_by(
                KnowledgeSupertagField.supertag_id.asc(),
                KnowledgeSupertagField.field_id.asc(),
            ).limit(limit + 1)
        else:
            stmt = stmt.limit(limit)
        rows = list((await session.execute(stmt)).scalars().all())
        next_cursor = None
        if paginate:
            rows, next_cursor = _split_page(
                rows,
                limit,
                lambda row: _encode_pull_cursor(
                    row.supertag_id,
                    row.field_id,
                    scope_digest=scope_fingerprint,
                    snapshot_digest=digest,
                    snapshot_token=snapshot_token,
                    scope_revision=effective_scope_revision,
                ),
            )
        result = {
            "changes": [serialize_docs_supertag_field(row) for row in rows],
            "tombstones": [],
            "cursor": next_cursor,
            "authoritative_count": len(digest_rows),
        }
        if next_cursor is None:
            if include_authoritative_ids:
                result["authoritative_ids"] = [
                    f"{supertag_id}:{field_id}"
                    for supertag_id, field_id, *_ in digest_rows
                ]
            result["authoritative_digest"] = digest
        return _annotate_docs_page(
            result,
            snapshot_revision=digest,
            snapshot_token=snapshot_token,
            scope_revision=effective_scope_revision,
        )

    if table == "knowledge_node_placements":
        # updated_at が無いため、可変列（sort_order/collapsed）をダイジェストへ織り込む。
        digest_rows = (
            await session.execute(
                select(
                    KnowledgeNodePlacement.id,
                    KnowledgeNodePlacement.node_id,
                    KnowledgeNodePlacement.parent_node_id,
                    KnowledgeNodePlacement.sort_order,
                    KnowledgeNodePlacement.collapsed,
                ).where(
                    KnowledgeNodePlacement.node_id.in_(visible_node_ids),
                    KnowledgeNodePlacement.parent_node_id.in_(visible_node_ids),
                )
            )
        ).all()
        digest = _docs_digest(
            [
                f"{id_}:{node_id}:{parent_node_id}:{sort_order!r}:{int(collapsed)}"
                for id_, node_id, parent_node_id, sort_order, collapsed in digest_rows
            ]
        )
        effective_force_full = _effective_force_full(
            force_full,
            client_digest,
            digest,
            user_id=user_id,
        )
        if not effective_force_full and client_digest is not None and client_digest == digest:
            return _annotate_docs_page(
                {
                    "changes": [],
                    "tombstones": [],
                    "cursor": None,
                    "authoritative_count": len(digest_rows),
                    "authoritative_digest": digest,
                    },
                snapshot_revision=digest,
                snapshot_token=snapshot_token,
                scope_revision=effective_scope_revision,
            )
        stmt = (
            select(KnowledgeNodePlacement)
            .where(
                KnowledgeNodePlacement.node_id.in_(visible_node_ids),
                KnowledgeNodePlacement.parent_node_id.in_(visible_node_ids),
            )
        )
        if paginate:
            cursor_parts = _decode_pull_cursor(
                cursor,
                1,
                scope_digest=scope_fingerprint,
                snapshot_digest=digest,
                snapshot_token=snapshot_token,
                scope_revision=effective_scope_revision,
            )
            if cursor_parts:
                stmt = stmt.where(KnowledgeNodePlacement.id > UUID(cursor_parts[0]))
            stmt = stmt.order_by(KnowledgeNodePlacement.id.asc()).limit(limit + 1)
        else:
            stmt = stmt.limit(limit)
        rows = list((await session.execute(stmt)).scalars().all())
        next_cursor = None
        if paginate:
            rows, next_cursor = _split_page(
                rows,
                limit,
                lambda row: _encode_pull_cursor(
                    row.id,
                    scope_digest=scope_fingerprint,
                    snapshot_digest=digest,
                    snapshot_token=snapshot_token,
                    scope_revision=effective_scope_revision,
                ),
            )
        result = {
            "changes": [serialize_docs_placement(row) for row in rows],
            "tombstones": [],
            "cursor": next_cursor,
            "authoritative_count": len(digest_rows),
        }
        if next_cursor is None:
            if include_authoritative_ids:
                result["authoritative_ids"] = [str(row[0]) for row in digest_rows]
            result["authoritative_digest"] = digest
        return _annotate_docs_page(
            result,
            snapshot_revision=digest,
            snapshot_token=snapshot_token,
            scope_revision=effective_scope_revision,
        )

    if table == "knowledge_edges":
        # updated_at が無いため、可変列（relation_type/confidence）をダイジェストへ織り込む。
        digest_rows = (
            await session.execute(
                select(
                    KnowledgeEdge.id,
                    KnowledgeEdge.source_node_id,
                    KnowledgeEdge.target_node_id,
                    KnowledgeEdge.relation_type,
                    KnowledgeEdge.confidence,
                ).where(
                    KnowledgeEdge.source_node_id.in_(visible_node_ids),
                    KnowledgeEdge.target_node_id.in_(visible_node_ids),
                )
            )
        ).all()
        digest = _docs_digest(
            [
                f"{id_}:{source_node_id}:{target_node_id}:{relation_type}:{confidence!r}"
                for id_, source_node_id, target_node_id, relation_type, confidence in digest_rows
            ]
        )
        effective_force_full = _effective_force_full(
            force_full,
            client_digest,
            digest,
            user_id=user_id,
        )
        if not effective_force_full and client_digest is not None and client_digest == digest:
            return _annotate_docs_page(
                {
                    "changes": [],
                    "tombstones": [],
                    "cursor": None,
                    "authoritative_count": len(digest_rows),
                    "authoritative_digest": digest,
                    },
                snapshot_revision=digest,
                snapshot_token=snapshot_token,
                scope_revision=effective_scope_revision,
            )
        stmt = (
            select(KnowledgeEdge)
            .where(
                KnowledgeEdge.source_node_id.in_(visible_node_ids),
                KnowledgeEdge.target_node_id.in_(visible_node_ids),
            )
        )
        if paginate:
            cursor_parts = _decode_pull_cursor(
                cursor,
                1,
                scope_digest=scope_fingerprint,
                snapshot_digest=digest,
                snapshot_token=snapshot_token,
                scope_revision=effective_scope_revision,
            )
            if cursor_parts:
                stmt = stmt.where(KnowledgeEdge.id > UUID(cursor_parts[0]))
            stmt = stmt.order_by(KnowledgeEdge.id.asc()).limit(limit + 1)
        else:
            stmt = stmt.limit(limit)
        rows = list((await session.execute(stmt)).scalars().all())
        next_cursor = None
        if paginate:
            rows, next_cursor = _split_page(
                rows,
                limit,
                lambda row: _encode_pull_cursor(
                    row.id,
                    scope_digest=scope_fingerprint,
                    snapshot_digest=digest,
                    snapshot_token=snapshot_token,
                    scope_revision=effective_scope_revision,
                ),
            )
        result = {
            "changes": [serialize_docs_edge(row) for row in rows],
            "tombstones": [],
            "cursor": next_cursor,
            "authoritative_count": len(digest_rows),
        }
        if next_cursor is None:
            if include_authoritative_ids:
                result["authoritative_ids"] = [str(row[0]) for row in digest_rows]
            result["authoritative_digest"] = digest
        return _annotate_docs_page(
            result,
            snapshot_revision=digest,
            snapshot_token=snapshot_token,
            scope_revision=effective_scope_revision,
        )

    return _annotate_docs_page(
        {"changes": [], "tombstones": [], "cursor": None},
        snapshot_revision=scope_fingerprint,
        snapshot_token=snapshot_token,
        scope_revision=effective_scope_revision,
    )


# ---------------------------------------------------------------------------
# push ディスパッチャ（REST の書き込みも push もここを通す）
# ---------------------------------------------------------------------------


class DocsOperationError(Exception):
    """Docs 書き込みの入力不正・not found を表す（status_code 付き）。"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


_SOURCE_REF_MAX_ITEMS = 64
_SOURCE_REF_MAX_KEYS = 24
_SOURCE_REF_MAX_STRING = 4096
_SOURCE_REF_MAX_BYTES = 64 * 1024
_SOURCE_REF_SECRET_KEY_RE = re.compile(
    r"(?:token|secret|password|passwd|api[_-]?key|access[_-]?key|authorization|"
    r"cookie|set-cookie|private[_-]?key|client[_-]?secret|signature|credential)",
    re.IGNORECASE,
)
_SOURCE_REF_PATH_KEY_RE = re.compile(
    r"(?:^|_)(?:path|file|filename|file_name|location|absolute_path|local_path)$",
    re.IGNORECASE,
)
_SOURCE_REF_ABSOLUTE_PATH_RE = re.compile(
    r"^(?:[A-Za-z]:[\\/]|[\\]{2}|/|~(?:[\\/]|$))"
)
_SOURCE_REF_URL_SCHEMES = {"http", "https", "memory"}
_SOURCE_REF_QUERY_SECRET_RE = re.compile(
    r"(?:token|secret|password|passwd|key|auth|signature|sig|cookie|credential)",
    re.IGNORECASE,
)


def _normalize_source_refs(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Validate optional sync provenance before it reaches a revision.

    ``DocsGraphService`` persists source refs in ``KnowledgeRevision``.  Sync
    clients are untrusted, so this boundary enforces a bounded, JSON-safe
    shape and rejects credentials or host paths rather than silently storing
    them.  Existing relative ``workspace_file`` references remain valid.
    """

    if "source_refs" not in payload:
        return None
    raw_refs = payload.get("source_refs")
    if raw_refs is None:
        return None
    if not isinstance(raw_refs, list):
        raise DocsOperationError("source_refsは配列で指定してください", status_code=400)
    if len(raw_refs) > _SOURCE_REF_MAX_ITEMS:
        raise DocsOperationError("source_refsの件数が上限を超えています", status_code=400)

    def clean(value: Any, *, key: str = "", depth: int = 0) -> Any:
        if depth > 3:
            raise DocsOperationError("source_refsの入れ子が深すぎます", status_code=400)
        if isinstance(value, str):
            if len(value) > _SOURCE_REF_MAX_STRING:
                raise DocsOperationError("source_refsの文字列が長すぎます", status_code=400)
            if any(ord(ch) < 0x20 and ch not in "\t\n\r" for ch in value):
                raise DocsOperationError("source_refsに制御文字を指定できません", status_code=400)
            if _SOURCE_REF_PATH_KEY_RE.search(key):
                path_text = value.strip()
                if _SOURCE_REF_ABSOLUTE_PATH_RE.match(path_text):
                    raise DocsOperationError("source_refsに絶対パスを指定できません", status_code=400)
                if any(part == ".." for part in re.split(r"[\\/]", path_text)):
                    raise DocsOperationError("source_refsのpath traversalは許可されません", status_code=400)
            if key.lower() == "sha256" and (
                len(value) != 64 or not re.fullmatch(r"[0-9a-fA-F]{64}", value)
            ):
                raise DocsOperationError("source_refsのsha256が不正です", status_code=400)
            is_url_key = key.lower() in {"url", "source_url", "href"}
            if is_url_key or "://" in value:
                parts = urlsplit(value)
                if parts.scheme:
                    if parts.scheme.lower() not in _SOURCE_REF_URL_SCHEMES:
                        raise DocsOperationError("source_refsのURL schemeが不正です", status_code=400)
                    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
                        raise DocsOperationError("source_refsにURL認証情報を指定できません", status_code=400)
                    for query_key, _query_value in parse_qsl(parts.query, keep_blank_values=True):
                        if _SOURCE_REF_QUERY_SECRET_RE.search(query_key):
                            raise DocsOperationError("source_refsのURL queryに秘密情報を指定できません", status_code=400)
                    scheme = parts.scheme.lower()
                    if scheme in {"http", "https"}:
                        query = urlencode(
                            sorted(
                                (query_key, query_value)
                                for query_key, query_value in parse_qsl(
                                    parts.query, keep_blank_values=True
                                )
                                if not query_key.lower().startswith("utm_")
                                and query_key.lower() not in {"fbclid", "gclid"}
                            )
                        )
                        path = parts.path.rstrip("/") or "/"
                        netloc = parts.netloc.lower()
                    else:
                        query = parts.query
                        path = parts.path
                        netloc = parts.netloc
                    # Fragments are not sent to the server and may contain
                    # OAuth/access tokens.  Persist only canonical provenance
                    # without a fragment, including harmless section anchors.
                    value = urlunsplit(
                        (scheme, netloc, path, query, "")
                    )
                elif is_url_key and value.strip():
                    raise DocsOperationError("source_refsのURL形式が不正です", status_code=400)
            return value
        if isinstance(value, float) and not math.isfinite(value):
            raise DocsOperationError("source_refsの数値が不正です", status_code=400)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, list):
            if len(value) > _SOURCE_REF_MAX_ITEMS:
                raise DocsOperationError("source_refs配列の要素数が上限を超えています", status_code=400)
            return [clean(item, key=key, depth=depth + 1) for item in value]
        if isinstance(value, dict):
            if len(value) > _SOURCE_REF_MAX_KEYS:
                raise DocsOperationError("source_refsの項目数が上限を超えています", status_code=400)
            result: dict[str, Any] = {}
            for raw_key, raw_value in value.items():
                if not isinstance(raw_key, str) or not raw_key.strip():
                    raise DocsOperationError("source_refsのkeyが不正です", status_code=400)
                key_text = raw_key.strip()
                if _SOURCE_REF_SECRET_KEY_RE.search(key_text):
                    raise DocsOperationError("source_refsに秘密情報を指定できません", status_code=400)
                result[key_text] = clean(raw_value, key=key_text, depth=depth + 1)
            return result
        raise DocsOperationError("source_refsにJSON以外の値を指定できません", status_code=400)

    normalized: list[dict[str, Any]] = []
    for raw in raw_refs:
        if not isinstance(raw, dict):
            raise DocsOperationError("source_refsの各要素はobjectで指定してください", status_code=400)
        normalized_value = clean(raw)
        assert isinstance(normalized_value, dict)
        if not normalized_value:
            raise DocsOperationError("空のsource_refsは指定できません", status_code=400)
        normalized.append(normalized_value)
    try:
        encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise DocsOperationError("source_refsのJSON形式が不正です", status_code=400) from exc
    try:
        encoded_size = len(encoded.encode("utf-8"))
    except UnicodeError as exc:
        raise DocsOperationError("source_refsのUnicode形式が不正です", status_code=400) from exc
    if encoded_size > _SOURCE_REF_MAX_BYTES:
        raise DocsOperationError("source_refsのサイズが上限を超えています", status_code=400)
    return normalized


async def _ensure_docs_project_allowed(
    service: DocsGraphService,
    project_id: UUID | None,
    user_id: UUID,
    *,
    docs_library_id: UUID | None = None,
) -> None:
    if project_id is None:
        return
    project = await service.session.get(Project, project_id)
    if project is None or project.deleted_at is not None:
        raise DocsOperationError("Projectが見つかりません", status_code=404)
    try:
        # Unified personal-library project nodes use Project ACL directly; no
        # personal owner/share or explicit membership-row side gate is
        # required here. ``has_permission`` below handles owner/admin and
        # member roles consistently.
        allowed = await ProjectRepository.has_permission(
            service.session,
            project_id=project_id,
            user_id=user_id,
            permission="write",
        )
    except Exception:
        allowed = False
    if not allowed:
        raise DocsOperationError(
            "Projectへの書き込み権限がありません",
            status_code=403,
        )
    if is_default_inbox_project(project):
        raise DocsOperationError(
            "InboxはDocsの案件保存先にできません。実案件を指定してください。",
            status_code=409,
        )


def _parse_base_updated_at(value: Optional[str]) -> Optional[datetime]:
    if value in (None, ""):
        return None
    normalized = str(value).strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise DocsOperationError("Invalid base_updated_at", status_code=400) from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _split_entity_id(entity_id: str) -> tuple[Optional[UUID], Optional[UUID]]:
    parts = str(entity_id or "").split(":", 1)
    return parse_uuid(parts[0]), parse_uuid(parts[1]) if len(parts) == 2 else None


async def load_current_docs_entity(
    session: AsyncSession,
    *,
    docs_library_id: UUID,
    table: str,
    entity_id: str,
    for_update: bool = False,
    user_id: UUID | None = None,
) -> Optional[dict[str, Any]]:
    """競合応答用に、サーバーの現在値と版を返す。"""
    if table == "knowledge_nodes":
        row = (
            await session.get(
                KnowledgeNode,
                parse_uuid(entity_id),
                with_for_update=for_update,
            )
            if parse_uuid(entity_id)
            else None
        )
        if row is not None and row.docs_library_id == docs_library_id and (
            user_id is None
            or await can_read_node(
                session,
                row,
                user_id,
                include_archived=True,
            )
        ):
            return serialize_docs_node(row)
        return {"id": entity_id, "deleted": True, "updated_at": None}
    if table == "knowledge_supertags":
        row = (
            await session.get(
                KnowledgeSupertag,
                parse_uuid(entity_id),
                with_for_update=for_update,
            )
            if parse_uuid(entity_id)
            else None
        )
        if row is not None and row.docs_library_id == docs_library_id:
            if user_id is not None:
                library = await session.get(DocsLibrary, docs_library_id)
                if library is None:
                    return {"id": entity_id, "deleted": True, "updated_at": None}
                if not await library_can_read(session, library, user_id):
                    linked = await session.scalar(
                        select(KnowledgeNodeSupertag.node_id)
                        .join(
                            KnowledgeNode,
                            KnowledgeNode.id == KnowledgeNodeSupertag.node_id,
                        )
                        .where(
                            KnowledgeNodeSupertag.supertag_id == row.id,
                            docs_readable_node_predicate(
                                KnowledgeNode,
                                docs_library_id=docs_library_id,
                                user_id=user_id,
                                library_owner_id=getattr(library, "owner_user_id", None),
                            ),
                        )
                        .limit(1)
                    )
                    if linked is None:
                        return {"id": entity_id, "deleted": True, "updated_at": None}
            return serialize_docs_supertag(row)
        return {"id": entity_id, "deleted": True, "updated_at": None}
    first, second = _split_entity_id(entity_id)
    if first is None or second is None:
        return None
    if table == "knowledge_node_supertags":
        node = await session.get(KnowledgeNode, first, with_for_update=for_update)
        tag = await session.get(KnowledgeSupertag, second)
        row = await session.get(
            KnowledgeNodeSupertag,
            {"node_id": first, "supertag_id": second},
            with_for_update=for_update,
        )
        if (
            node is not None
            and node.docs_library_id == docs_library_id
            and tag is not None
            and tag.docs_library_id == docs_library_id
            and row is not None
            and (user_id is None or await can_read_node(session, node, user_id, include_archived=True))
        ):
            return serialize_docs_node_supertag(row)
        return {
            "node_id": str(first),
            "supertag_id": str(second),
            "deleted": True,
            # 関連行自体に版がない時の削除版。同期書き込みは親ノードを
            # touch するため、古い端末の再作成を拒否できる。
            "updated_at": _iso(node.updated_at) if node is not None else None,
        }
    if table == "knowledge_field_values":
        node = await session.get(KnowledgeNode, first, with_for_update=for_update)
        field = await session.get(KnowledgeField, second)
        row = await session.get(
            KnowledgeFieldValue,
            {"node_id": first, "field_id": second},
            with_for_update=for_update,
        )
        if (
            node is not None
            and node.docs_library_id == docs_library_id
            and field is not None
            and field.docs_library_id == docs_library_id
            and row is not None
            and (user_id is None or await can_read_node(session, node, user_id, include_archived=True))
            and (
                user_id is None
                or row.target_node_id is None
                or await can_read_node(
                    session,
                    row.target_node_id,
                    user_id,
                    include_archived=True,
                )
            )
        ):
            return serialize_docs_field_value(row)
        return {
            "node_id": str(first),
            "field_id": str(second),
            "deleted": True,
            "updated_at": _iso(node.updated_at) if node is not None else None,
        }
    return None


async def ensure_docs_operation_is_fresh(
    session: AsyncSession,
    *,
    docs_library_id: UUID,
    table: str,
    action: str,
    entity_id: str,
    base_updated_at: Optional[str],
    require_base_version: bool = False,
    user_id: UUID | None = None,
) -> None:
    """端末時計ではなく、サーバー行のupdated_atを基準に楽観ロックする。"""
    base = _parse_base_updated_at(base_updated_at)
    if base is None and not require_base_version:
        return
    entity = await load_current_docs_entity(
        session,
        docs_library_id=docs_library_id,
        table=table,
        entity_id=entity_id,
        for_update=require_base_version or base is not None,
        user_id=user_id,
    )
    if not entity:
        return
    deleted = entity.get("deleted") is True
    if base is None:
        if deleted and action in ("create", "update"):
            return
        if deleted and action == "delete":
            return
        if deleted:
            raise DocsOperationError(
                "Docs entity was deleted on the server",
                status_code=409,
            )
        if not require_base_version:
            return
        raise DocsOperationError(
            "Docs base_updated_at is required for an existing entity",
            status_code=409,
        )
    current = _parse_base_updated_at(entity.get("updated_at"))
    if deleted and action == "delete":
        return
    if current is None and deleted:
        raise DocsOperationError(
            "Docs entity was deleted on the server",
            status_code=409,
        )
    if current is not None and current != base:
        raise DocsOperationError(
            "Docs entity was updated on the server",
            status_code=409,
        )


async def _get_node(service: DocsGraphService, docs_library_id: UUID, ref: str) -> KnowledgeNode:
    try:
        return await service.resolve_node(
            docs_library_id=docs_library_id, ref=str(ref), allow_archived=True
        )
    except ValueError as exc:
        raise DocsOperationError(str(exc), status_code=404) from exc


async def _active_project_pointer(
    session: AsyncSession,
    node_id: UUID,
    *,
    for_update: bool = True,
) -> Project | None:
    """Return the live Project reverse-pointer for a Docs node.

    ``projects.knowledge_node_id`` is denormalized identity metadata.  Generic
    Docs mutations lock and inspect it immediately before dispatch so an
    owner cannot archive/move/delete the canonical root through the offline
    sync path while a Project API operation is concurrently repairing it.
    """

    stmt = (
        select(Project)
        .where(
            Project.knowledge_node_id == node_id,
            Project.deleted_at.is_(None),
        )
        .limit(1)
    )
    if for_update:
        stmt = stmt.with_for_update()
    result = await session.execute(stmt)
    # Defensive ``first`` avoids a malformed database with duplicate pointer
    # rows turning a protected root into a 500 before the denial is returned.
    return result.scalars().first()


async def _load_canonical_project_node(
    service: DocsGraphService,
    project_id: UUID,
) -> KnowledgeNode:
    """Load the already-initialized canonical Project Information node.

    This is a read-only validator used by generic parent/move checks.  Create
    callers must use ``ensure_project_information_doc`` first; generic update
    and reparent paths must never repair a stale pointer as a side effect.
    """

    from ..services.docs_workspace import get_project_docs_library

    project = await service.session.get(Project, project_id)
    if project is None or project.deleted_at is not None:
        raise DocsOperationError("Projectが見つかりません", status_code=404)
    library = await get_project_docs_library(
        service.session,
        project_id=project_id,
        actor_user_id=None,
    )
    pointer_id = project.knowledge_node_id
    node = await service.session.get(KnowledgeNode, pointer_id) if pointer_id else None
    if (
        library is None
        or node is None
        or node.docs_library_id != library.id
        or str(getattr(library, "library_type", "personal") or "personal").lower()
        != "personal"
        or library.owner_user_id != project.owner_id
        or node.project_id != project_id
        or node.archived_at is not None
        or node.system_key != f"project_information:{project_id}"
        or node.parent_id is None
        or node.root_page_id != node.parent_id
    ):
        raise DocsOperationError(
            "Project Docsのcanonical rootが未初期化または不正です",
            status_code=409,
        )
    return node


async def _ensure_canonical_project_parent(
    service: DocsGraphService,
    *,
    project_id: UUID,
    user_id: UUID,
    docs_library_id: UUID,
) -> KnowledgeNode:
    """Ensure the dedicated Project Information parent for generic create."""

    await _ensure_docs_project_allowed(
        service,
        project_id,
        user_id,
        docs_library_id=docs_library_id,
    )
    from ..services.project_information_docs import ensure_project_information_doc

    project = await service.session.get(Project, project_id)
    if project is None:
        raise DocsOperationError("Projectが見つかりません", status_code=404)
    try:
        node = await ensure_project_information_doc(
            service.session,
            project=project,
            user_id=user_id,
        )
    except PermissionError as exc:
        raise DocsOperationError(str(exc), status_code=403) from exc
    except ValueError as exc:
        raise DocsOperationError(str(exc), status_code=409) from exc
    if node.docs_library_id != docs_library_id:
        raise DocsOperationError(
            "Project Docsは案件所有者のPersonal Docs Libraryに限られます",
            status_code=400,
        )
    return await _load_canonical_project_node(service, project_id)


async def _validate_project_parent(
    service: DocsGraphService,
    *,
    parent: KnowledgeNode,
    project_id: UUID,
    docs_library_id: UUID,
    canonical_root: KnowledgeNode | None = None,
) -> KnowledgeNode:
    """Require a parent to be inside the project's canonical subtree."""

    canonical_root = canonical_root or await _load_canonical_project_node(
        service,
        project_id,
    )
    if (
        parent.docs_library_id != docs_library_id
        or parent.project_id != project_id
        or parent.archived_at is not None
        or (
            parent.id != canonical_root.id
            and parent.root_page_id != canonical_root.root_page_id
        )
    ):
        raise DocsOperationError(
            "指定された親nodeはProject Docsの正規サブツリーではありません",
            status_code=400,
        )
    return canonical_root


def _is_unscoped_navigation_parent(parent: KnowledgeNode) -> bool:
    """Return whether a null-project create would target a navigation shell.

    Personal ``案件情報`` and legacy ``Home`` roots are metadata shells, not
    ordinary user document containers.  A null ``project_id`` must not be
    used to smuggle arbitrary Docs below either shell; Project creates are
    routed to their canonical project root instead.
    """

    system_key = str(getattr(parent, "system_key", "") or "").strip().casefold()
    if system_key in {"project_information_root", "home"}:
        return True
    # Historical Home/hub rows may predate system_key.  Limit title matching
    # to a top-level root so an ordinary child document named "Home" remains
    # a valid Personal container.
    if getattr(parent, "parent_id", None) is None:
        title = str(getattr(parent, "title", "") or "").strip().casefold()
        if title in {"home", "案件情報", "案件情報ハブ", "project information"}:
            return True
    return False


async def _touch_docs_child_version(
    service: DocsGraphService,
    node: KnowledgeNode,
    user_id: UUID,
) -> None:
    """関連行の削除後にも比較できる、親ノード由来のサーバー版を発行する。"""
    node.updated_by = user_id
    node.updated_at = datetime.utcnow()
    await service.session.flush()


async def _update_app_readme_from_docs(
    service: DocsGraphService,
    *,
    node: KnowledgeNode,
    user_id: UUID,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Write the canonical README when an app_readme Docs node is edited."""
    from ..memory.models import App
    from ..services.app_git_service import AppGitError, AppGitService
    from ..services.app_operation_lock import app_operation_lock
    from ..services.app_service import AppAccessError, AppService
    from ..services.app_storage import get_workspaces_root, resolve_app_file, sha256_file

    # ロック path を決める root と、実際に触る README の root がずれると
    # 排他が静かに壊れる（別ロックで同じファイルを触る最悪形）。この操作で
    # 使う実効 root をここで 1 度だけ確定し、以降のすべての App 呼び出しへ
    # 同じ値を渡す。``service.workspace_root`` が未設定なら既定 root を解決する。
    workspace_root = get_workspaces_root(getattr(service, "workspace_root", None))

    body_json = payload.get("body_json")
    markdown = body_json.get("markdown") if isinstance(body_json, dict) else None
    if not isinstance(markdown, str):
        raise DocsOperationError(
            "app_readme の編集には body_json.markdown が必要です",
            status_code=400,
        )
    app = await service.session.scalar(select(App).where(App.id == node.app_id).limit(1))
    if app is None:
        raise DocsOperationError("App READMEの正本が見つかりません", status_code=404)
    try:
        await AppService(workspace_root=workspace_root).require_permission(
            service.session,
            app,
            user_id=user_id,
            required="developer",
        )
    except AppAccessError as exc:
        raise DocsOperationError(str(exc), status_code=403) from exc
    async with app_operation_lock(app.id, workspace_root=workspace_root):
        readme_path = resolve_app_file(app.id, "README.md", workspace_root=workspace_root)
        current_hash = sha256_file(readme_path) if readme_path.exists() else None
        props = node.display_props if isinstance(node.display_props, dict) else {}
        expected_hash = props.get("app_readme_sha256")
        if not expected_hash:
            previous = node.body_json if isinstance(node.body_json, dict) else {}
            previous_markdown = previous.get("markdown")
            if isinstance(previous_markdown, str):
                expected_hash = hashlib.sha256(previous_markdown.encode("utf-8")).hexdigest()
        if expected_hash and current_hash != expected_hash:
            raise DocsOperationError(
                "README.mdがDocsの編集開始後に変更されています。先に競合を解決してください",
                status_code=409,
            )
        # README.md が正本、Docs Node は同期ビュー。
        # checkpoint / Node同期のどちらかが失敗したら、書き込み前の README を
        # 復元してから例外を投げる。ファイルだけ新しく Git と Docs が旧い、
        # という中途半端な状態を残さないための補償処理。
        previous_bytes = readme_path.read_bytes() if readme_path.exists() else None
        readme_path.parent.mkdir(parents=True, exist_ok=True)
        readme_path.write_text(markdown, encoding="utf-8", newline="\n")
        try:
            try:
                AppGitService(workspace_root=workspace_root).checkpoint(
                    app.id, "DocsからApp READMEを更新", actor=str(user_id)
                )
            except AppGitError as exc:
                raise DocsOperationError(
                    f"App READMEのGit checkpointに失敗しました: {exc}", status_code=503
                ) from exc
            await AppService(workspace_root=workspace_root).sync_readme_to_node(
                service.session, app, user_id
            )
        except BaseException:
            _restore_app_readme(readme_path, previous_bytes)
            raise
        return serialize_docs_node(node)


def _restore_app_readme(readme_path: Path, previous_bytes: bytes | None) -> None:
    """README.md を書き込み前の状態へ戻す。復元自体の失敗はログに残すだけにする。"""
    try:
        if previous_bytes is None:
            readme_path.unlink(missing_ok=True)
        else:
            readme_path.write_bytes(previous_bytes)
    except OSError:
        logger.exception("App READMEの復元に失敗しました: %s", readme_path)


async def apply_docs_operation(
    session: AsyncSession,
    service: DocsGraphService,
    *,
    user_id: UUID,
    docs_library_id: UUID | None = None,
    workspace_id: UUID | None = None,
    table: str,
    action: str,
    entity_id: str,
    payload: dict[str, Any],
    base_updated_at: Optional[str] = None,
    require_base_version: bool = False,
) -> dict[str, Any]:
    """Docs 書き込みの単一入口。派生更新は DocsGraphService に委譲し、commit まで行う。

    戻り値はシリアライズ済み entity。REST/push で共有する（ロジック二重化ゼロ）。
    """
    payload = payload or {}
    if docs_library_id is None:
        docs_library_id = workspace_id
    if docs_library_id is None:
        raise ValueError("docs_library_id is required")

    # Authorization is checked immediately before dispatch, in the same
    # transaction as the mutation. Individual graph methods repeat the check
    # after locking/refreshing the target row to close revoke-vs-write races.
    await _assert_docs_operation_acl(
        session,
        docs_library_id=docs_library_id,
        table=table,
        action=action,
        entity_id=entity_id,
        payload=payload,
        user_id=user_id,
    )

    await ensure_docs_operation_is_fresh(
        session,
        docs_library_id=docs_library_id,
        table=table,
        action=action,
        entity_id=entity_id,
        base_updated_at=base_updated_at,
        require_base_version=require_base_version,
        user_id=user_id,
    )

    if table == "knowledge_nodes":
        result = await _apply_node_operation(
            service, docs_library_id=docs_library_id, user_id=user_id,
            action=action, entity_id=entity_id, payload=payload,
        )
    elif table == "knowledge_node_supertags":
        result = await _apply_node_supertag_operation(
            service, docs_library_id=docs_library_id, user_id=user_id,
            action=action, entity_id=entity_id, payload=payload,
        )
    elif table == "knowledge_field_values":
        result = await _apply_field_value_operation(
            service, session, docs_library_id=docs_library_id, user_id=user_id,
            action=action, entity_id=entity_id, payload=payload,
        )
    elif table == "knowledge_supertags":
        result = await _apply_supertag_operation(
            service, docs_library_id=docs_library_id, user_id=user_id,
            action=action, entity_id=entity_id, payload=payload,
        )
    else:
        raise DocsOperationError(f"Unsupported docs table: {table}", status_code=400)

    # Push responses use the same ACL metadata as pull responses.  Without
    # this enrichment a successful offline mutation would overwrite the
    # mobile row's source/access/read_only fields with the legacy serializer's
    # nulls, allowing edits to continue after a share is revoked.
    if table == "knowledge_nodes":
        node_id = parse_uuid(result.get("id") if isinstance(result, dict) else None)
        if node_id is None:
            node_id = parse_uuid(entity_id)
        if node_id is not None:
            node_row = await session.get(KnowledgeNode, node_id)
            node_workspace = (
                await session.get(DocsLibrary, node_row.docs_library_id)
                if node_row is not None
                else None
            )
            if node_row is not None and node_workspace is not None:
                result = await serialize_docs_node_for_sync(
                    session,
                    node_row,
                    library=node_workspace,
                    user_id=user_id,
                )

    await session.commit()
    return result


async def _assert_docs_operation_acl(
    session: AsyncSession,
    *,
    docs_library_id: UUID,
    table: str,
    action: str,
    entity_id: str,
    payload: dict[str, Any],
    user_id: UUID,
) -> None:
    """Fail closed before a Docs mutation can touch any derived row."""

    library = await session.get(DocsLibrary, docs_library_id)
    if library is None:
        raise DocsOperationError("Docs library not found", status_code=404)

    if table == "knowledge_nodes" and action == "create":
        requested_project_id = parse_uuid(payload.get("project_id"))
        if (
            "project_id" in payload
            and payload.get("project_id") not in (None, "")
            and requested_project_id is None
        ):
            raise DocsOperationError("Invalid project_id", status_code=400)
        parent_id = parse_uuid(payload.get("parent_id"))
        if parent_id is not None:
            parent = await session.get(KnowledgeNode, parent_id)
            if parent is None or parent.docs_library_id != docs_library_id:
                raise DocsOperationError("親nodeが見つかりません", status_code=404)
            if not await can_write_node(session, parent, user_id, library=library):
                raise DocsOperationError("Docs nodeへの書き込み権限がありません", status_code=403)
            if requested_project_id is None and _is_unscoped_navigation_parent(parent):
                raise DocsOperationError(
                    "Project未指定のDocs nodeはPersonalの案件情報hub/Home配下に作成できません",
                    status_code=400,
                )
        elif requested_project_id is not None:
            # An explicit Project create without a parent is routed through the
            # dedicated Project Information ensure path by
            # ``_ensure_canonical_project_parent``.  Do not require owner-only
            # Personal-library write access at this generic ACL gate.
            project = await session.get(Project, requested_project_id)
            if project is None or project.deleted_at is not None:
                raise DocsOperationError("Projectが見つかりません", status_code=404)
            try:
                allowed = await ProjectRepository.has_permission(
                    session,
                    project_id=requested_project_id,
                    user_id=user_id,
                    permission="write",
                )
            except Exception:
                allowed = False
            if not allowed:
                raise DocsOperationError("Projectへの書き込み権限がありません", status_code=403)
            if is_default_inbox_project(project):
                raise DocsOperationError(
                    "InboxはDocsの案件保存先にできません。実案件を指定してください。",
                    status_code=409,
                )
        elif not await library_can_write(session, library, user_id):
            raise DocsOperationError("Docs workspaceへの書き込み権限がありません", status_code=403)
        return

    node_id: UUID | None = None
    if table == "knowledge_nodes":
        node_id = parse_uuid(entity_id)
    elif table in {"knowledge_node_supertags", "knowledge_field_values"}:
        node_id = parse_uuid(payload.get("node_id")) or _split_entity_id(entity_id)[0]
    if node_id is not None:
        node = await session.get(KnowledgeNode, node_id)
        if node is None or node.docs_library_id != docs_library_id:
            raise DocsOperationError("Docs node not found", status_code=404)
        if not await can_write_node(
            session,
            node,
            user_id,
            library=library,
            include_archived=True,
        ):
            raise DocsOperationError("Docs nodeへの書き込み権限がありません", status_code=403)
        return

    # Supertag definitions are library-scoped metadata. Keep them behind
    # the library/project write ACL even when no node is supplied.
    if table == "knowledge_supertags" and action == "create":
        if not await library_can_write(session, library, user_id):
            raise DocsOperationError("Docs workspaceへの書き込み権限がありません", status_code=403)
        return

    raise DocsOperationError("Docs entity not found", status_code=404)


async def _apply_node_operation(
    service: DocsGraphService,
    *,
    docs_library_id: UUID,
    user_id: UUID,
    action: str,
    entity_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    # Provenance is optional, but when supplied it is persisted on the
    # resulting KnowledgeRevision.  Validate once at the mutation boundary so
    # recursive archive→update operations use the same sanitized shape.
    source_refs = _normalize_source_refs(payload)

    async def assert_generic_allowed(node: KnowledgeNode, tool_name: str) -> None:
        from ..services.managed_docs_policy import assert_managed_docs_tree_mutation_allowed

        try:
            await assert_managed_docs_tree_mutation_allowed(
                service.session, node, tool_name=tool_name
            )
        except PermissionError as exc:
            raise DocsOperationError(str(exc), status_code=403) from exc

    if action == "create":
        node_id = parse_uuid(payload.get("id")) or parse_uuid(entity_id)
        title = str(payload.get("title") or "").strip()
        node_type = str(payload.get("node_type") or "node")
        # Validate the full blank discriminator before resolving parents or
        # project roots.  Only an ordinary ``node`` with the canonical
        # paragraph marker may carry an empty title.
        try:
            body_json = normalize_docs_body_json(payload.get("body_json"))
        except ValueError as exc:
            raise DocsOperationError(str(exc), status_code=400) from exc
        explicit_blank = is_explicit_blank_paragraph(title, body_json, node_type)
        if not title and not explicit_blank:
            raise DocsOperationError("空行はDocs nodeとして保存できません", status_code=400)
        parent = None
        parent_ref = payload.get("parent_id")
        requested_project_id = parse_uuid(payload.get("project_id"))
        if (
            "project_id" in payload
            and payload.get("project_id") not in (None, "")
            and requested_project_id is None
        ):
            raise DocsOperationError("Invalid project_id", status_code=400)
        canonical_project_root: KnowledgeNode | None = None
        if parent_ref:
            parent = await _get_node(service, docs_library_id, parent_ref)
            await assert_generic_allowed(parent, "docs_rest_create")
        if requested_project_id is not None:
            # Explicit Project creates always resolve the canonical Project
            # Information parent first.  This mirrors the Next route's
            # dedicated ensure path and prevents a caller from attaching
            # Project B content to a Personal/Home hub or Project A subtree.
            canonical_project_root = await _ensure_canonical_project_parent(
                service,
                project_id=requested_project_id,
                user_id=user_id,
                docs_library_id=docs_library_id,
            )
            if parent is None:
                parent = canonical_project_root
            elif parent.project_id != requested_project_id:
                raise DocsOperationError(
                    "親nodeと異なるProjectには関連付けられません",
                    status_code=400,
                )
            await _validate_project_parent(
                service,
                parent=parent,
                project_id=requested_project_id,
                docs_library_id=docs_library_id,
                canonical_root=canonical_project_root,
            )
            effective_project_id = requested_project_id
        elif parent is not None and parent.project_id is not None:
            # Omitting project_id is allowed only when inheriting an existing
            # canonical Project subtree parent, never when selecting an
            # arbitrary same-library Project-looking node.
            await _ensure_docs_project_allowed(
                service,
                parent.project_id,
                user_id,
                docs_library_id=docs_library_id,
            )
            await _validate_project_parent(
                service,
                parent=parent,
                project_id=parent.project_id,
                docs_library_id=docs_library_id,
            )
            effective_project_id = parent.project_id
        elif parent is not None and _is_unscoped_navigation_parent(parent):
            raise DocsOperationError(
                "Project未指定のDocs nodeはPersonalの案件情報hub/Home配下に作成できません",
                status_code=400,
            )
        else:
            effective_project_id = None
        await _ensure_docs_project_allowed(
            service,
            effective_project_id,
            user_id,
            docs_library_id=docs_library_id,
        )
        try:
            node = await service.create_node(
                docs_library_id=docs_library_id,
                user_id=user_id,
                title=title,
                parent=parent,
                project_id=effective_project_id,
                body_json=body_json,
                node_type=node_type,
                sort_order=payload.get("sort_order"),
                node_id=node_id,
                day_date=_parse_date(payload.get("day_date")),
                source_refs=source_refs,
            )
        except ValueError as exc:
            raise DocsOperationError(str(exc), status_code=400) from exc
        description = payload.get("description")
        if description is not None:
            node.description = str(description)[:200000]
            await service.session.flush()
        return serialize_docs_node(node)

    if action in ("archive", "delete"):
        node = await _get_node(service, docs_library_id, entity_id)
        if await _active_project_pointer(service.session, node.id):
            raise DocsOperationError(
                "アクティブProjectのcanonical情報rootは通常のDocs操作でアーカイブ/削除できません",
                status_code=409,
            )
        await assert_generic_allowed(node, "docs_rest_archive")
        # オフラインで update → archive が統合された場合も、更新内容を先に
        # 同じサーバー版へ適用してからアーカイブする。
        if payload:
            await _apply_node_operation(
                service,
                docs_library_id=docs_library_id,
                user_id=user_id,
                action="update",
                entity_id=entity_id,
                payload=payload,
            )
            node = await _get_node(service, docs_library_id, entity_id)
        # node だけ archive すると、outline から消えたのに検索へ残る孤児ができる。
        await service.archive_subtree(root=node, user_id=user_id)
        return serialize_docs_node(node)

    if action == "move":
        node = await _get_node(service, docs_library_id, entity_id)
        if await _active_project_pointer(service.session, node.id):
            raise DocsOperationError(
                "アクティブProjectのcanonical情報rootは通常のDocs操作で移動できません",
                status_code=409,
            )
        await assert_generic_allowed(node, "docs_rest_move")
        new_parent_ref = payload.get("new_parent_id") or payload.get("parent_id")
        if not new_parent_ref:
            raise DocsOperationError("new_parent_id is required", status_code=400)
        new_parent = await _get_node(service, docs_library_id, new_parent_ref)
        await assert_generic_allowed(new_parent, "docs_rest_move")
        node_project_id = parse_uuid(node.project_id)
        parent_project_id = parse_uuid(new_parent.project_id)
        await _ensure_docs_project_allowed(
            service,
            parent_project_id or node_project_id,
            user_id,
            docs_library_id=docs_library_id,
        )
        if parent_project_id != node_project_id:
            raise DocsOperationError(
                "親nodeと異なるProjectには移動できません",
                status_code=400,
            )
        if node_project_id is not None:
            await _validate_project_parent(
                service,
                parent=new_parent,
                project_id=node_project_id,
                docs_library_id=docs_library_id,
            )
        try:
            await service.move_node(
                node=node,
                new_parent=new_parent,
                user_id=user_id,
                leave_reference=bool(payload.get("leave_reference")),
            )
        except ValueError as exc:
            raise DocsOperationError(str(exc), status_code=400) from exc
        if payload.get("sort_order") is not None:
            node.sort_order = float(payload["sort_order"])
            await service.session.flush()
        return serialize_docs_node(node)

    if action == "update":
        body_json_provided = "body_json" in payload and payload.get("body_json") is not None
        try:
            normalized_body_json = (
                normalize_docs_body_json(payload.get("body_json"))
                if body_json_provided
                else None
            )
        except ValueError as exc:
            raise DocsOperationError(str(exc), status_code=400) from exc
        node = await _get_node(service, docs_library_id, entity_id)
        active_project_pointer = await _active_project_pointer(service.session, node.id)
        await assert_generic_allowed(node, "docs_rest_update")
        requested_title = (
            str(payload.get("title") or "").strip()
            if "title" in payload
            else None
        )
        if requested_title is not None and not requested_title:
            if not is_explicit_blank_paragraph(
                requested_title,
                normalized_body_json,
                getattr(node, "node_type", "node"),
            ):
                raise DocsOperationError("空行はDocs nodeとして保存できません", status_code=400)
        current_project_id = parse_uuid(node.project_id)
        requested_project_id = (
            parse_uuid(payload.get("project_id"))
            if "project_id" in payload
            else current_project_id
        )
        if (
            "project_id" in payload
            and payload.get("project_id") not in (None, "")
            and requested_project_id is None
        ):
            raise DocsOperationError("Invalid project_id", status_code=400)
        if requested_project_id != current_project_id:
            if active_project_pointer is not None:
                raise DocsOperationError(
                    "アクティブProjectのcanonical情報rootのProject identityは変更できません",
                    status_code=409,
                )
            if current_project_id is None and requested_project_id is not None:
                raise DocsOperationError(
                    "Docs nodeのProject identityは通常の更新では変更できません（案件nodeをDocsルートにはできません）",
                    status_code=400,
                )
            raise DocsOperationError(
                "Docs nodeのProject identityは通常の更新では変更できません",
                status_code=400,
            )
        if active_project_pointer is not None and "project_id" in payload:
            # Even a no-op assignment is a generic identity operation on the
            # reverse-pointer root.  Keep all Project identity lifecycle work
            # behind the dedicated Project Information API.
            raise DocsOperationError(
                "アクティブProjectのcanonical情報rootのProject identityは変更できません",
                status_code=409,
            )
        # A canonical reverse-pointer root may still receive title/body edits,
        # but its parent/identity lifecycle belongs to Project Information
        # APIs, never generic sync operations.
        if active_project_pointer is not None and "parent_id" in payload:
            raise DocsOperationError(
                "アクティブProjectのcanonical情報rootは通常のDocs操作で移動できません",
                status_code=409,
            )
        if (
            "parent_id" in payload
            and payload.get("parent_id") not in (None, "")
            and parse_uuid(payload.get("parent_id")) is None
        ):
            raise DocsOperationError("Invalid parent_id", status_code=400)
        if node.app_id and node.node_type == "app_readme" and "body_json" in payload:
            return await _update_app_readme_from_docs(
                service,
                node=node,
                user_id=user_id,
                payload=payload,
            )
        final_parent = None
        if "parent_id" in payload:
            requested_parent_id = parse_uuid(payload.get("parent_id"))
            if requested_parent_id is not None:
                final_parent = await _get_node(service, docs_library_id, str(requested_parent_id))
        elif node.parent_id is not None:
            final_parent = await _get_node(service, docs_library_id, str(node.parent_id))
        if final_parent is not None:
            await assert_generic_allowed(final_parent, "docs_rest_update")

        if final_parent is not None:
            parent_project_id = parse_uuid(final_parent.project_id)
            if parent_project_id != current_project_id and (
                active_project_pointer is None
                or "parent_id" in payload
                or "project_id" in payload
            ):
                raise DocsOperationError(
                    "親nodeと異なるProjectには移動できません",
                    status_code=400,
                )
            if current_project_id is not None and (
                "parent_id" in payload or "project_id" in payload
            ):
                await _validate_project_parent(
                    service,
                    parent=final_parent,
                    project_id=current_project_id,
                    docs_library_id=docs_library_id,
                )
        effective_project_id = current_project_id
        if effective_project_id is not None and final_parent is None:
            raise DocsOperationError("案件nodeをDocsルートにはできません", status_code=400)
        await _ensure_docs_project_allowed(
            service,
            effective_project_id,
            user_id,
            docs_library_id=docs_library_id,
        )

        # parent_id 変更はインデント/アウトデント/移動を包含する。
        if "parent_id" in payload:
            new_parent_id = parse_uuid(payload.get("parent_id"))
            if new_parent_id is None:
                if node.parent_id is not None:
                    node.parent_id = None
                    node.root_page_id = None
                    node.updated_by = user_id
                    node.updated_at = datetime.utcnow()
                    await service.record_node_change(node, user_id, "nodeをトップレベルへ移動")
                    await service.session.flush()
            elif new_parent_id != node.parent_id:
                try:
                    await service.move_node(
                        node=node,
                        new_parent=final_parent,
                        user_id=user_id,
                        leave_reference=bool(payload.get("leave_reference")),
                    )
                except ValueError as exc:
                    raise DocsOperationError(str(exc), status_code=400) from exc
        has_content = any(
            key in payload for key in ("title", "description", "body_json", "source_refs")
        )
        if has_content:
            try:
                body_json = normalized_body_json
                await service.update_node(
                    node=node,
                    user_id=user_id,
                    title=payload.get("title"),
                    description=payload.get("description"),
                    body_json=body_json,
                    source_refs=source_refs,
                )
            except ValueError as exc:
                raise DocsOperationError(str(exc), status_code=400) from exc
        # スカラー属性（parent 移動時に move が sort_order を再設定するため後で上書き）。
        touched = False
        if payload.get("sort_order") is not None:
            node.sort_order = float(payload["sort_order"])
            touched = True
        if "project_id" in payload:
            # The identity check above allows only a no-op assignment for
            # compatibility with older mobile payloads.
            node.project_id = current_project_id
            touched = True
        if "day_date" in payload:
            node.day_date = _parse_date(payload.get("day_date"))
            touched = True
        if touched:
            node.updated_by = user_id
            node.updated_at = datetime.utcnow()
            await service.session.flush()
            if "project_id" in payload:
                await service.upsert_search_index(node)
                from ..rag.docs_index import enqueue_docs_reindex

                enqueue_docs_reindex(node.docs_library_id, node.id)
        return serialize_docs_node(node)

    raise DocsOperationError(f"Unsupported node action: {action}", status_code=400)


async def _apply_node_supertag_operation(
    service: DocsGraphService,
    *,
    docs_library_id: UUID,
    user_id: UUID,
    action: str,
    entity_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    node_ref = payload.get("node_id") or (entity_id.split(":")[0] if entity_id else None)
    if not node_ref:
        raise DocsOperationError("node_id is required", status_code=400)
    node = await _get_node(service, docs_library_id, node_ref)
    from ..services.managed_docs_policy import assert_managed_docs_tree_mutation_allowed

    try:
        await assert_managed_docs_tree_mutation_allowed(
            service.session,
            node,
            tool_name="docs_sync_node_supertag",
        )
    except PermissionError as exc:
        raise DocsOperationError(str(exc), status_code=403) from exc

    if action == "create":
        supertag_id = parse_uuid(payload.get("supertag_id"))
        if supertag_id is not None:
            tag = await service.resolve_supertag(
                docs_library_id=docs_library_id, tag=str(supertag_id), create=False
            )
        else:
            name = str(payload.get("name") or "").strip()
            if not name:
                raise DocsOperationError("supertag_id or name is required", status_code=400)
            tag = await service.resolve_supertag(docs_library_id=docs_library_id, tag=name, create=True)
        changed = await service.add_tag(node=node, tag=tag, user_id=user_id)
        if changed:
            await _touch_docs_child_version(service, node, user_id)
        link = await service.session.get(
            KnowledgeNodeSupertag, {"node_id": node.id, "supertag_id": tag.id}
        )
        return serialize_docs_node_supertag(link) if link is not None else {
            "node_id": str(node.id), "supertag_id": str(tag.id),
        }

    if action == "delete":
        supertag_ref = payload.get("supertag_id") or (
            entity_id.split(":")[1] if entity_id and ":" in entity_id else None
        )
        if not supertag_ref:
            raise DocsOperationError("supertag_id is required", status_code=400)
        try:
            tag = await service.resolve_supertag(
                docs_library_id=docs_library_id, tag=str(supertag_ref), create=False
            )
        except ValueError as exc:
            raise DocsOperationError(str(exc), status_code=404) from exc
        changed = await service.remove_tag(node=node, tag=tag, user_id=user_id)
        if changed:
            await _touch_docs_child_version(service, node, user_id)
        return {
            "node_id": str(node.id),
            "supertag_id": str(tag.id),
            "deleted": True,
            "updated_at": _iso(node.updated_at),
        }

    raise DocsOperationError(f"Unsupported node_supertag action: {action}", status_code=400)


async def _apply_field_value_operation(
    service: DocsGraphService,
    session: AsyncSession,
    *,
    docs_library_id: UUID,
    user_id: UUID,
    action: str,
    entity_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if action not in ("update", "create"):
        raise DocsOperationError(f"Unsupported field_value action: {action}", status_code=400)
    node_ref = payload.get("node_id") or (entity_id.split(":")[0] if entity_id else None)
    field_ref = payload.get("field_id") or (
        entity_id.split(":")[1] if entity_id and ":" in entity_id else None
    )
    if not node_ref or not field_ref:
        raise DocsOperationError("node_id and field_id are required", status_code=400)
    field_id = parse_uuid(field_ref)
    if field_id is None:
        raise DocsOperationError("invalid field_id", status_code=400)
    node = await _get_node(service, docs_library_id, node_ref)
    from ..services.managed_docs_policy import assert_managed_docs_tree_mutation_allowed

    try:
        await assert_managed_docs_tree_mutation_allowed(
            service.session,
            node,
            tool_name="docs_sync_field_value",
        )
    except PermissionError as exc:
        raise DocsOperationError(str(exc), status_code=403) from exc
    value = payload.get("value")
    try:
        await service.set_field_by_id(node=node, field_id=field_id, value=value, user_id=user_id)
    except ValueError as exc:
        raise DocsOperationError(str(exc), status_code=400) from exc
    await _touch_docs_child_version(service, node, user_id)
    row = await session.get(KnowledgeFieldValue, {"node_id": node.id, "field_id": field_id})
    if row is None:
        return {
            "node_id": str(node.id),
            "field_id": str(field_id),
            "deleted": True,
            "updated_at": _iso(node.updated_at),
        }
    return serialize_docs_field_value(row)


async def _apply_supertag_operation(
    service: DocsGraphService,
    *,
    docs_library_id: UUID,
    user_id: UUID,
    action: str,
    entity_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if action != "create":
        raise DocsOperationError(f"Unsupported supertag action: {action}", status_code=400)
    name = str(payload.get("name") or "").strip()
    if not name:
        raise DocsOperationError("name is required", status_code=400)
    supertag_id = parse_uuid(payload.get("id")) or parse_uuid(entity_id)
    tag = KnowledgeSupertag(
        docs_library_id=docs_library_id,
        name=name[:120],
        base_type=str(payload.get("base_type") or "note"),
        color=payload.get("color") or "#64748b",
        icon=payload.get("icon"),
        template_json={},
        pinned_field_ids=[],
        config_json={},
    )
    if supertag_id is not None:
        tag.id = supertag_id
    service.session.add(tag)
    await service.session.flush()
    return serialize_docs_supertag(tag)
