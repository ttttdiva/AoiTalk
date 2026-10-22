"""Sync v3 の scope 計算（読み取り専用）。

既存の可視性判定を再利用する:
- Project: ``sync_routes._accessible_project_ids``（共有 Project ACL）
- Space: ``/api/spaces`` 一覧と同じ ``SpaceAccessPolicy``（Inbox は所有者のみ、
  通常 Space は所有者・admin・Project read 経由の member）
- Docs: ``sync_routes._docs_sync_scopes``（Personal/Project/共有）

書き込み（commit、ensure_user_inbox_setup、Docs library の作成）は一切しない。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.sync_routes import _accessible_project_ids, _docs_sync_scopes
from ..memory.models import (
    DocsLibrary,
    KnowledgeNode,
    KnowledgeNodeShare,
    Project,
    ProjectMember,
    Space,
    User,
)
from ..memory.project_repository import ProjectRepository
from ..services.space_access import space_access_policy
from .contract import SCOPE_TABLES, ScopeFilter, docs_scope_key


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _scope(
    *,
    kind: str,
    scope_key: str,
    entity_id: str,
    project_id: str | None,
    access: str,
    read_only: bool,
    fingerprint_material: dict[str, Any],
) -> dict[str, Any]:
    return {
        "scope_key": scope_key,
        "kind": kind,
        "id": entity_id,
        "project_id": project_id,
        "read_only": bool(read_only),
        "access": access,
        "tables": list(SCOPE_TABLES[kind]),
        "fingerprint": _sha256_json({"kind": kind, "scope_key": scope_key, **fingerprint_material}),
    }


async def _user_role(session: AsyncSession, user_id: UUID) -> str:
    role = (await session.execute(select(User.role).where(User.id == user_id))).scalar_one_or_none()
    return str(role or "")


async def _project_scopes(
    session: AsyncSession, user_id: UUID, project_ids: list[UUID]
) -> list[dict[str, Any]]:
    if not project_ids:
        return []
    member_rows = await session.execute(
        select(ProjectMember.project_id, ProjectMember.role).where(
            ProjectMember.user_id == user_id, ProjectMember.project_id.in_(project_ids)
        )
    )
    roles = {project_id: role for project_id, role in member_rows.all()}
    owner_rows = await session.execute(
        select(Project.id, Project.owner_id).where(Project.id.in_(project_ids))
    )
    owners = {project_id: owner_id for project_id, owner_id in owner_rows.all()}
    scopes = []
    for project_id in sorted(set(project_ids), key=str):
        can_write = await ProjectRepository.has_permission(
            session, project_id=project_id, user_id=user_id, permission="write"
        )
        access = "write" if can_write else "read"
        scopes.append(
            _scope(
                kind="project",
                scope_key=f"project:{project_id}",
                entity_id=str(project_id),
                project_id=str(project_id),
                access=access,
                read_only=access == "read",
                fingerprint_material={
                    "access": access,
                    "read_only": access == "read",
                    "role": roles.get(project_id),
                    "owner": owners.get(project_id) == user_id,
                },
            )
        )
    return scopes


async def visible_spaces(session: AsyncSession, user_id: UUID) -> list[tuple[Space, bool]]:
    """``/api/spaces`` 一覧と同じ可視判定で (Space, can_write) を返す。"""
    user_info = {"role": await _user_role(session, user_id)}
    is_admin = space_access_policy.is_admin_user(user_info)
    member_space_ids = await space_access_policy.member_space_ids(session, user_id)
    result = await session.execute(
        select(Space).order_by(Space.sort_order.asc(), Space.created_at.asc())
    )
    visible: list[tuple[Space, bool]] = []
    for space in result.scalars().all():
        if space_access_policy.is_inbox_space(space):
            if space.owner_id != user_id:
                continue
        elif not (space.owner_id == user_id or is_admin or space.id in member_space_ids):
            continue
        can_write, _ = await space_access_policy.can_write_space(
            session, space_id=space.id, user_id=user_id, user_info=user_info
        )
        visible.append((space, bool(can_write)))
    return visible


async def _space_scopes(session: AsyncSession, user_id: UUID) -> list[dict[str, Any]]:
    scopes = []
    for space, can_write in await visible_spaces(session, user_id):
        access = "write" if can_write else "read"
        scopes.append(
            _scope(
                kind="space",
                scope_key=f"space:{space.id}",
                entity_id=str(space.id),
                project_id=None,
                access=access,
                read_only=access == "read",
                fingerprint_material={"access": access, "read_only": access == "read"},
            )
        )
    return scopes


async def _docs_acl_entries(
    session: AsyncSession, user_id: UUID, library_id: UUID, project_id: UUID | None
) -> list[list[str]]:
    """docs_scope_revision と同じ node 共有エントリ（v2 pull の acl_stmt と同条件）。"""
    stmt = (
        select(KnowledgeNodeShare.node_id, KnowledgeNodeShare.permission)
        .join(KnowledgeNode, KnowledgeNode.id == KnowledgeNodeShare.node_id)
        .where(
            KnowledgeNodeShare.user_id == user_id,
            KnowledgeNode.docs_library_id == library_id,
        )
    )
    if project_id is not None:
        stmt = stmt.where(KnowledgeNode.project_id == project_id)
    rows = (await session.execute(stmt)).all()
    return sorted([str(node_id), str(permission or "").lower()] for node_id, permission in rows)


async def _docs_scopes(
    session: AsyncSession, user_id: UUID, project_ids: list[UUID]
) -> list[dict[str, Any]]:
    personal_library = (
        await session.execute(
            select(DocsLibrary)
            .where(DocsLibrary.owner_user_id == user_id, DocsLibrary.library_type == "personal")
            .order_by(DocsLibrary.created_at.asc(), DocsLibrary.id.asc())
            .limit(1)
        )
    ).scalars().first()
    raw_scopes = await _docs_sync_scopes(
        session, user_id=user_id, project_ids=project_ids, personal_library=personal_library
    )
    scopes = []
    seen: set[str] = set()
    for raw in raw_scopes:
        library_id = str(raw.get("docs_library_id") or raw.get("workspace_id"))
        project_id = raw.get("project_id")
        key = docs_scope_key(library_id, project_id)
        if key in seen:
            continue
        seen.add(key)
        access = str(raw.get("access") or "read")
        read_only = bool(raw.get("read_only"))
        acl = await _docs_acl_entries(
            session, user_id, UUID(library_id), UUID(project_id) if project_id else None
        )
        scopes.append(_scope(
            kind="docs",
            scope_key=key,
            entity_id=library_id,
            project_id=str(project_id) if project_id else None,
            access=access,
            read_only=read_only,
            fingerprint_material={
                "access": access,
                "read_only": read_only,
                "source": raw.get("source"),
                "acl": acl,
            },
        ))
    return scopes


async def compute_scopes(
    session: AsyncSession, user_id: UUID | str
) -> tuple[str, list[dict[str, Any]], ScopeFilter]:
    """(scope_set_revision, scopes, ScopeFilter) を返す。"""
    actor = UUID(str(user_id))
    project_ids = await _accessible_project_ids(session, actor)
    scopes: list[dict[str, Any]] = [
        _scope(
            kind="user",
            scope_key=f"user:{actor}",
            entity_id=str(actor),
            project_id=None,
            access="owner",
            read_only=False,
            fingerprint_material={"access": "owner", "read_only": False},
        )
    ]
    # user scope（会話）の指紋は、会話の可視性判定に使う閲覧可能 project_id の集合だけで決まる。
    scopes[0]["fingerprint"] = hashlib.sha256(
        ("conv-acl:v1\n" + "\n".join(sorted(str(pid) for pid in set(project_ids)))).encode("utf-8")
    ).hexdigest()
    scopes.extend(await _space_scopes(session, actor))
    scopes.extend(await _project_scopes(session, actor, project_ids))
    scopes.extend(await _docs_scopes(session, actor, project_ids))
    revision = _sha256_json([[scope["scope_key"], scope["fingerprint"]] for scope in sorted(
        scopes, key=lambda item: item["scope_key"])])
    scope_filter = ScopeFilter(
        user_id=actor,
        project_ids=tuple(sorted(set(project_ids), key=str)),
        docs_library_ids=tuple(
            sorted({UUID(scope["id"]) for scope in scopes if scope["kind"] == "docs"}, key=str)
        ),
        space_ids=tuple(
            sorted({UUID(scope["id"]) for scope in scopes if scope["kind"] == "space"}, key=str)
        ),
    )
    return revision, scopes, scope_filter
