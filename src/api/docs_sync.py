"""Docs 同期・書き込みの単一実装。

`sync_routes.py`（/api/sync/pull・push）と `docs_routes.py`（/api/docs/*）の両方から
import される。書き込みはすべて ``apply_docs_operation`` を通り、派生更新は
``DocsGraphService`` に集約される（不変条件 ``docs/docs_editing_invariants.md``）。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import or_, select
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
    Project,
    ProjectMember,
    User,
)
from ..services.docs_graph_service import DocsGraphService
from ..services.project_information_docs import is_default_inbox_project
from ..utils.uuid_utils import parse_uuid

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


def _normalize_node_type(value: Any) -> str:
    text = str(value or "node")
    return text if text in _VALID_NODE_TYPES else "node"


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


# ---------------------------------------------------------------------------
# シリアライザ（Web knowledge-docs-utils.ts の serialize* と同一キー・同一意味）
# ---------------------------------------------------------------------------


def serialize_docs_node(row: KnowledgeNode) -> dict[str, Any]:
    node_type = _normalize_node_type(row.node_type)
    query_json = row.query_json if isinstance(row.query_json, dict) else None
    return {
        "id": str(row.id),
        "workspace_id": str(row.workspace_id) if row.workspace_id else None,
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


def serialize_docs_supertag(row: KnowledgeSupertag) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "workspace_id": str(row.workspace_id) if row.workspace_id else None,
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


def serialize_docs_field(row: KnowledgeField) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "workspace_id": str(row.workspace_id) if row.workspace_id else None,
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
# pull クエリ（workspace 単位・updated_at 差分 / 関連表は権威セット全量）
# ---------------------------------------------------------------------------


async def pull_docs_table(
    table: str,
    session: AsyncSession,
    *,
    workspace_id: UUID,
    accessible_project_ids: list[UUID],
    since: Optional[datetime],
) -> dict[str, Any]:
    """Docs 8 テーブルの pull。sync_routes._pull_table から委譲される。"""
    limit = DOCS_PULL_LIMITS.get(table, 5000)
    project_access = (
        or_(
            KnowledgeNode.project_id.is_(None),
            KnowledgeNode.project_id.in_(accessible_project_ids),
        )
        if accessible_project_ids
        else KnowledgeNode.project_id.is_(None)
    )
    visible_node_ids = select(KnowledgeNode.id).where(
        KnowledgeNode.workspace_id == workspace_id,
        project_access,
    )

    if table == "knowledge_nodes":
        stmt = select(KnowledgeNode).where(
            KnowledgeNode.workspace_id == workspace_id,
            project_access,
        )
        if since:
            stmt = stmt.where(
                or_(KnowledgeNode.updated_at > since, KnowledgeNode.archived_at > since)
            )
        stmt = stmt.order_by(KnowledgeNode.updated_at.desc()).limit(limit)
        rows = list((await session.execute(stmt)).scalars().all())
        ids_result = await session.execute(
            visible_node_ids
        )
        # アーカイブは archived_at 付き通常行として changes で返す（tombstone にしない）。
        # ハード削除は行そのものが残らないため、workspace の権威 ID 集合も返して
        # モバイル側の古い SQLite cache を reconcile する。
        return {
            "changes": [serialize_docs_node(row) for row in rows],
            "tombstones": [],
            "cursor": None,
            "authoritative_ids": [str(item) for item in ids_result.scalars().all()],
            "authoritative_scope_id": str(workspace_id),
        }

    if table == "knowledge_supertags":
        stmt = select(KnowledgeSupertag).where(KnowledgeSupertag.workspace_id == workspace_id)
        if since:
            stmt = stmt.where(KnowledgeSupertag.updated_at > since)
        stmt = stmt.order_by(KnowledgeSupertag.updated_at.desc()).limit(limit)
        rows = list((await session.execute(stmt)).scalars().all())
        return {
            "changes": [serialize_docs_supertag(row) for row in rows],
            "tombstones": [],
            "cursor": None,
        }

    if table == "knowledge_fields":
        stmt = select(KnowledgeField).where(KnowledgeField.workspace_id == workspace_id)
        if since:
            stmt = stmt.where(KnowledgeField.updated_at > since)
        stmt = stmt.order_by(KnowledgeField.updated_at.desc()).limit(limit)
        rows = list((await session.execute(stmt)).scalars().all())
        return {
            "changes": [serialize_docs_field(row) for row in rows],
            "tombstones": [],
            "cursor": None,
        }

    if table == "knowledge_field_values":
        # value クリア（行削除）が Web/他クライアントで起きても伝播するよう、関連表と同じ
        # authoritative_ids 方式（毎回権威セット全量）で pull する。updated_at 差分では
        # サーバに存在しない行（削除済み）をモバイルが検知できないため。
        stmt = (
            select(KnowledgeFieldValue)
            .join(KnowledgeNode, KnowledgeNode.id == KnowledgeFieldValue.node_id)
            .where(KnowledgeFieldValue.node_id.in_(visible_node_ids))
            .limit(limit)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        changes = [serialize_docs_field_value(row) for row in rows]
        ids_result = await session.execute(
            select(KnowledgeFieldValue.node_id, KnowledgeFieldValue.field_id)
            .join(KnowledgeNode, KnowledgeNode.id == KnowledgeFieldValue.node_id)
            .where(KnowledgeFieldValue.node_id.in_(visible_node_ids))
        )
        return {
            "changes": changes,
            "tombstones": [],
            "cursor": None,
            "authoritative_ids": [
                f"{node_id}:{field_id}" for node_id, field_id in ids_result.all()
            ],
        }

    # 関連表も updated_at を返すが、削除を伝播できるよう毎回権威セット全量
    # + authoritative_ids で reconcile する。
    if table == "knowledge_node_supertags":
        stmt = (
            select(KnowledgeNodeSupertag)
            .join(KnowledgeNode, KnowledgeNode.id == KnowledgeNodeSupertag.node_id)
            .where(KnowledgeNodeSupertag.node_id.in_(visible_node_ids))
            .limit(limit)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        changes = [serialize_docs_node_supertag(row) for row in rows]
        ids_result = await session.execute(
            select(KnowledgeNodeSupertag.node_id, KnowledgeNodeSupertag.supertag_id)
            .join(KnowledgeNode, KnowledgeNode.id == KnowledgeNodeSupertag.node_id)
            .where(KnowledgeNodeSupertag.node_id.in_(visible_node_ids))
        )
        return {
            "changes": changes,
            "tombstones": [],
            "cursor": None,
            "authoritative_ids": [
                f"{node_id}:{supertag_id}"
                for node_id, supertag_id in ids_result.all()
            ],
        }

    if table == "knowledge_supertag_fields":
        stmt = (
            select(KnowledgeSupertagField)
            .join(KnowledgeSupertag, KnowledgeSupertag.id == KnowledgeSupertagField.supertag_id)
            .where(KnowledgeSupertag.workspace_id == workspace_id)
            .limit(limit)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        changes = [serialize_docs_supertag_field(row) for row in rows]
        ids_result = await session.execute(
            select(KnowledgeSupertagField.supertag_id, KnowledgeSupertagField.field_id)
            .join(KnowledgeSupertag, KnowledgeSupertag.id == KnowledgeSupertagField.supertag_id)
            .where(KnowledgeSupertag.workspace_id == workspace_id)
        )
        return {
            "changes": changes,
            "tombstones": [],
            "cursor": None,
            "authoritative_ids": [
                f"{supertag_id}:{field_id}"
                for supertag_id, field_id in ids_result.all()
            ],
        }

    if table == "knowledge_node_placements":
        stmt = (
            select(KnowledgeNodePlacement)
            .where(
                KnowledgeNodePlacement.node_id.in_(visible_node_ids),
                KnowledgeNodePlacement.parent_node_id.in_(visible_node_ids),
            )
            .limit(limit)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        ids_result = await session.execute(
            select(KnowledgeNodePlacement.id)
            .where(
                KnowledgeNodePlacement.node_id.in_(visible_node_ids),
                KnowledgeNodePlacement.parent_node_id.in_(visible_node_ids),
            )
        )
        changes = [serialize_docs_placement(row) for row in rows]
        return {
            "changes": changes,
            "tombstones": [],
            "cursor": None,
            "authoritative_ids": [str(item) for item in ids_result.scalars().all()],
        }

    if table == "knowledge_edges":
        stmt = (
            select(KnowledgeEdge)
            .where(
                KnowledgeEdge.source_node_id.in_(visible_node_ids),
                KnowledgeEdge.target_node_id.in_(visible_node_ids),
            )
            .limit(limit)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        ids_result = await session.execute(
            select(KnowledgeEdge.id)
            .where(
                KnowledgeEdge.source_node_id.in_(visible_node_ids),
                KnowledgeEdge.target_node_id.in_(visible_node_ids),
            )
        )
        changes = [serialize_docs_edge(row) for row in rows]
        return {
            "changes": changes,
            "tombstones": [],
            "cursor": None,
            "authoritative_ids": [str(item) for item in ids_result.scalars().all()],
        }

    return {"changes": [], "tombstones": [], "cursor": None}


# ---------------------------------------------------------------------------
# push ディスパッチャ（REST の書き込みも push もここを通す）
# ---------------------------------------------------------------------------


class DocsOperationError(Exception):
    """Docs 書き込みの入力不正・not found を表す（status_code 付き）。"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


async def _ensure_docs_project_allowed(
    service: DocsGraphService,
    project_id: UUID | None,
    user_id: UUID,
) -> None:
    if project_id is None:
        return
    project = await service.session.get(Project, project_id)
    if project is None or project.deleted_at is not None:
        raise DocsOperationError("Projectが見つかりません", status_code=404)
    membership = (
        await service.session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    user = await service.session.get(User, user_id)
    if membership is None or (
        membership.role not in {"admin", "owner"}
        and getattr(user, "role", None) != "admin"
    ):
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
    workspace_id: UUID,
    table: str,
    entity_id: str,
    for_update: bool = False,
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
        if row is not None and row.workspace_id == workspace_id:
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
        if row is not None and row.workspace_id == workspace_id:
            return serialize_docs_supertag(row)
        return {"id": entity_id, "deleted": True, "updated_at": None}
    first, second = _split_entity_id(entity_id)
    if first is None or second is None:
        return None
    if table == "knowledge_node_supertags":
        node = await session.get(KnowledgeNode, first, with_for_update=for_update)
        row = await session.get(
            KnowledgeNodeSupertag,
            {"node_id": first, "supertag_id": second},
            with_for_update=for_update,
        )
        if node is not None and node.workspace_id == workspace_id and row is not None:
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
        row = await session.get(
            KnowledgeFieldValue,
            {"node_id": first, "field_id": second},
            with_for_update=for_update,
        )
        if node is not None and node.workspace_id == workspace_id and row is not None:
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
    workspace_id: UUID,
    table: str,
    action: str,
    entity_id: str,
    base_updated_at: Optional[str],
    require_base_version: bool = False,
) -> None:
    """端末時計ではなく、サーバー行のupdated_atを基準に楽観ロックする。"""
    base = _parse_base_updated_at(base_updated_at)
    if base is None and not require_base_version:
        return
    entity = await load_current_docs_entity(
        session,
        workspace_id=workspace_id,
        table=table,
        entity_id=entity_id,
        for_update=require_base_version or base is not None,
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


async def _get_node(service: DocsGraphService, workspace_id: UUID, ref: str) -> KnowledgeNode:
    try:
        return await service.resolve_node(
            workspace_id=workspace_id, ref=str(ref), allow_archived=True
        )
    except ValueError as exc:
        raise DocsOperationError(str(exc), status_code=404) from exc


async def _touch_docs_child_version(
    service: DocsGraphService,
    node: KnowledgeNode,
    user_id: UUID,
) -> None:
    """関連行の削除後にも比較できる、親ノード由来のサーバー版を発行する。"""
    node.updated_by = user_id
    node.updated_at = datetime.utcnow()
    await service.session.flush()


async def apply_docs_operation(
    session: AsyncSession,
    service: DocsGraphService,
    *,
    user_id: UUID,
    workspace_id: UUID,
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

    await ensure_docs_operation_is_fresh(
        session,
        workspace_id=workspace_id,
        table=table,
        action=action,
        entity_id=entity_id,
        base_updated_at=base_updated_at,
        require_base_version=require_base_version,
    )

    if table == "knowledge_nodes":
        result = await _apply_node_operation(
            service, workspace_id=workspace_id, user_id=user_id,
            action=action, entity_id=entity_id, payload=payload,
        )
    elif table == "knowledge_node_supertags":
        result = await _apply_node_supertag_operation(
            service, workspace_id=workspace_id, user_id=user_id,
            action=action, entity_id=entity_id, payload=payload,
        )
    elif table == "knowledge_field_values":
        result = await _apply_field_value_operation(
            service, session, workspace_id=workspace_id, user_id=user_id,
            action=action, entity_id=entity_id, payload=payload,
        )
    elif table == "knowledge_supertags":
        result = await _apply_supertag_operation(
            service, workspace_id=workspace_id, user_id=user_id,
            action=action, entity_id=entity_id, payload=payload,
        )
    else:
        raise DocsOperationError(f"Unsupported docs table: {table}", status_code=400)

    await session.commit()
    return result


async def _apply_node_operation(
    service: DocsGraphService,
    *,
    workspace_id: UUID,
    user_id: UUID,
    action: str,
    entity_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if action == "create":
        node_id = parse_uuid(payload.get("id")) or parse_uuid(entity_id)
        parent = None
        parent_ref = payload.get("parent_id")
        if parent_ref:
            parent = await _get_node(service, workspace_id, parent_ref)
        requested_project_id = parse_uuid(payload.get("project_id"))
        effective_project_id = (
            parent.project_id if parent is not None and parent.project_id is not None
            else requested_project_id
        )
        if (
            parent is not None
            and parent.project_id is not None
            and requested_project_id is not None
            and requested_project_id != parent.project_id
        ):
            raise DocsOperationError(
                "親nodeと異なるProjectには関連付けられません",
                status_code=400,
            )
        if effective_project_id is not None and parent is None:
            raise DocsOperationError("案件nodeをDocsルートにはできません", status_code=400)
        await _ensure_docs_project_allowed(service, effective_project_id, user_id)
        title = str(payload.get("title") or "").strip()
        node = await service.create_node(
            workspace_id=workspace_id,
            user_id=user_id,
            title=title or "Untitled",
            parent=parent,
            project_id=effective_project_id,
            body_json=payload.get("body_json") if isinstance(payload.get("body_json"), dict) else None,
            node_type=str(payload.get("node_type") or "node"),
            sort_order=payload.get("sort_order"),
            node_id=node_id,
            day_date=_parse_date(payload.get("day_date")),
        )
        description = payload.get("description")
        if description is not None:
            node.description = str(description)[:200000]
            await service.session.flush()
        return serialize_docs_node(node)

    if action in ("archive", "delete"):
        node = await _get_node(service, workspace_id, entity_id)
        # オフラインで update → archive が統合された場合も、更新内容を先に
        # 同じサーバー版へ適用してからアーカイブする。
        if payload:
            await _apply_node_operation(
                service,
                workspace_id=workspace_id,
                user_id=user_id,
                action="update",
                entity_id=entity_id,
                payload=payload,
            )
            node = await _get_node(service, workspace_id, entity_id)
        await service.archive_node(node=node, user_id=user_id)
        return serialize_docs_node(node)

    if action == "move":
        node = await _get_node(service, workspace_id, entity_id)
        new_parent_ref = payload.get("new_parent_id") or payload.get("parent_id")
        if not new_parent_ref:
            raise DocsOperationError("new_parent_id is required", status_code=400)
        new_parent = await _get_node(service, workspace_id, new_parent_ref)
        await _ensure_docs_project_allowed(
            service,
            new_parent.project_id or node.project_id,
            user_id,
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
        node = await _get_node(service, workspace_id, entity_id)
        final_parent = None
        if "parent_id" in payload:
            requested_parent_id = parse_uuid(payload.get("parent_id"))
            if requested_parent_id is not None:
                final_parent = await _get_node(service, workspace_id, str(requested_parent_id))
        elif node.parent_id is not None:
            final_parent = await _get_node(service, workspace_id, str(node.parent_id))

        requested_project_id = (
            parse_uuid(payload.get("project_id"))
            if "project_id" in payload
            else node.project_id
        )
        if final_parent is not None and final_parent.project_id is not None:
            if (
                "project_id" in payload
                and requested_project_id is not None
                and requested_project_id != final_parent.project_id
            ):
                raise DocsOperationError(
                    "親nodeと異なるProjectには関連付けられません",
                    status_code=400,
                )
            effective_project_id = final_parent.project_id
        else:
            effective_project_id = requested_project_id
        if effective_project_id is not None and final_parent is None:
            raise DocsOperationError("案件nodeをDocsルートにはできません", status_code=400)
        await _ensure_docs_project_allowed(service, effective_project_id, user_id)

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
            key in payload for key in ("title", "description", "body_json")
        )
        if has_content:
            await service.update_node(
                node=node,
                user_id=user_id,
                title=payload.get("title"),
                description=payload.get("description"),
                body_json=payload.get("body_json") if isinstance(payload.get("body_json"), dict) else None,
            )
        # スカラー属性（parent 移動時に move が sort_order を再設定するため後で上書き）。
        touched = False
        if payload.get("sort_order") is not None:
            node.sort_order = float(payload["sort_order"])
            touched = True
        if "project_id" in payload:
            node.project_id = effective_project_id
            touched = True
        if "day_date" in payload:
            node.day_date = _parse_date(payload.get("day_date"))
            touched = True
        if touched:
            node.updated_by = user_id
            node.updated_at = datetime.utcnow()
            await service.session.flush()
        return serialize_docs_node(node)

    raise DocsOperationError(f"Unsupported node action: {action}", status_code=400)


async def _apply_node_supertag_operation(
    service: DocsGraphService,
    *,
    workspace_id: UUID,
    user_id: UUID,
    action: str,
    entity_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    node_ref = payload.get("node_id") or (entity_id.split(":")[0] if entity_id else None)
    if not node_ref:
        raise DocsOperationError("node_id is required", status_code=400)
    node = await _get_node(service, workspace_id, node_ref)

    if action == "create":
        supertag_id = parse_uuid(payload.get("supertag_id"))
        if supertag_id is not None:
            tag = await service.resolve_supertag(
                workspace_id=workspace_id, tag=str(supertag_id), create=False
            )
        else:
            name = str(payload.get("name") or "").strip()
            if not name:
                raise DocsOperationError("supertag_id or name is required", status_code=400)
            tag = await service.resolve_supertag(workspace_id=workspace_id, tag=name, create=True)
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
                workspace_id=workspace_id, tag=str(supertag_ref), create=False
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
    workspace_id: UUID,
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
    node = await _get_node(service, workspace_id, node_ref)
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
    workspace_id: UUID,
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
        workspace_id=workspace_id,
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
