"""Shared service layer for AoiTalk Docs graph operations."""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable

from sqlalchemy import String, and_, case, cast, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..memory.models import (
    KnowledgeEdge,
    KnowledgeField,
    KnowledgeFieldValue,
    KnowledgeNode,
    KnowledgeNodePlacement,
    KnowledgeNodeSupertag,
    KnowledgeRevision,
    KnowledgeSearchIndex,
    KnowledgeSupertag,
    DocsLibrary,
    Project,
    Task,
)
from ..memory.project_repository import ProjectRepository
from ..task_time import DEFAULT_TASK_TIMEZONE
from .docs_workspace import (
    ensure_docs_library,
    ensure_project_docs_library,
    get_project_docs_library,
)
from .docs_acl import (
    _shared_nodes_cte,
    apply_docs_visibility,
    can_read_node,
    can_write_node,
    docs_readable_node_predicate,
    library_can_write,
)
from .clip_ingest_policy import is_film_docs_node
from .docs_scope import DocsScope
from .task_management_service import TaskManagementService


SYSTEM_TASK_TAG = "task"
TASK_FIELD_TO_TASK_UPDATE = {
    "task_status": "status",
    "task_due": "end_at",
    "task_start": "start_at",
    "task_priority": "priority",
    "task_project": "project_id",
}

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_NODE_TOKEN_RE = re.compile(
    r"\[\[node:([0-9a-fA-F-]{36})(?:\|[^\]]*)?\]\]|@docs:([0-9a-fA-F-]{36})"
)
_TAG_TOKEN_RE = re.compile(r"(?:^|\s)#([^\s#:\[]+)")
_FIELD_TOKEN_RE = re.compile(r"([^|#\n]{1,80})::\s*([^|#\n]+)")


def is_explicit_blank_paragraph(
    title: Any,
    body_json: Any,
    node_type: Any = "node",
) -> bool:
    """Return whether a Docs row uses the canonical persisted blank paragraph.

    Empty titles are intentionally *not* generally valid Docs nodes.  The one
    exception is an ordinary ``node`` whose metadata explicitly identifies a
    paragraph block and carries the boolean ``blank`` marker.  Keep this
    predicate strict (``is True`` rather than truthiness) so malformed payloads
    such as ``"true"`` cannot create an indistinguishable blank row.
    """

    # Callers normalize user input before reaching this predicate.  Keep the
    # predicate itself strict so the persisted representation is exactly
    # title=""; whitespace-only values must not become canonical by accident.
    if title != "":
        return False
    if str(node_type or "") != "node" or not isinstance(body_json, dict):
        return False
    return (
        body_json.get("format") == "doc_block"
        and body_json.get("block_type") == "paragraph"
        and body_json.get("blank") is True
    )


def blank_paragraph_body_json(existing_body_json: Any = None) -> dict[str, Any]:
    """Return metadata carrying the canonical blank paragraph marker.

    Existing metadata is copied and retained; only the three canonical
    discriminators are overwritten.  Callers can therefore transition a
    normal paragraph to blank without dropping provenance/display metadata.
    """

    result = dict(existing_body_json) if isinstance(existing_body_json, dict) else {}
    result.update(format="doc_block", block_type="paragraph", blank=True)
    return result


def clear_blank_paragraph_marker(existing_body_json: Any = None) -> dict[str, Any]:
    """Copy body metadata while removing only the persisted blank marker."""

    result = dict(existing_body_json) if isinstance(existing_body_json, dict) else {}
    result.pop("blank", None)
    return result


@dataclass(frozen=True)
class ParsedOutlineLine:
    depth: int
    title: str
    tags: tuple[str, ...]
    fields: dict[str, str]


def _now() -> datetime:
    return datetime.utcnow()


def _title_mirror(title: Any) -> str:
    """title 由来の検索ミラー本文を返す（不変条件: 改行禁止・500字以内）。

    Web `docsNodeTitleMirror`（docs-node-writer.ts）と同一挙動。body_text は本文正本
    ではなく title のミラーであり、Web↔モバイル往復で検索インデックス・暗号化を
    一致させるためここで一元生成する。
    """
    mirror = str(title or "").strip()
    if "\n" in mirror or "\r" in mirror:
        raise ValueError("Docs node body_text mirror must not contain newlines")
    return mirror[:500]


def docs_searchable_body_text(body_text: Any, body_json: Any = None) -> str:
    """Return the user-visible body text used by the lexical Docs index.

    Ordinary outline nodes keep the historical ``body_text`` title mirror.
    Typed Markdown/code blocks are the one intentional exception: their
    independent editable payload lives in ``body_json.content`` and must be
    searchable instead of exposing only the label/title mirror.  Keep this
    helper deliberately strict so unrelated/system ``body_json`` formats do
    not accidentally become searchable content.
    """

    if isinstance(body_json, dict):
        if (
            body_json.get("format") == "doc_block"
            and body_json.get("block_type") in {"markdown", "code"}
            and isinstance(body_json.get("content"), str)
        ):
            return body_json["content"]
    return str(body_text or "")


def normalize_docs_title_identity(value: Any) -> str:
    """Compare labels without treating ordinary/full-width spacing as new text."""
    return re.sub(r"\s+", " ", str(value or "").replace("\u3000", " ")).strip().lower()


def _short_id(value: uuid.UUID | str | None) -> str:
    return str(value or "")[:8]


def _coerce_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return uuid.UUID(text)
    except (TypeError, ValueError):
        return None


def _resolve_docs_library_id(
    docs_library_id: uuid.UUID | None,
    workspace_id: uuid.UUID | None,
) -> uuid.UUID:
    """Resolve the canonical library ID with a legacy workspace alias.

    The 0019 wire/API contract is ``docs_library_id``.  During rolling deploys
    Python callers and mobile sync may still send ``workspace_id``; accepting
    it at this service boundary keeps old clients read/write compatible while
    preventing a silent query with a null scope.
    """

    value = docs_library_id if docs_library_id is not None else workspace_id
    if value is None:
        raise ValueError("docs_library_id is required")
    return value


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _parse_outline_text(outline_text: str) -> list[ParsedOutlineLine]:
    lines: list[ParsedOutlineLine] = []
    for raw_line in str(outline_text or "").replace("\r\n", "\n").splitlines():
        if not raw_line.strip():
            continue
        expanded = raw_line.replace("    ", "\t")
        depth = 0
        while depth < len(expanded) and expanded[depth] == "\t":
            depth += 1
        content = expanded[depth:].strip()
        if not content:
            continue
        content = re.sub(r"^(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|\[[ xX]\]\s+)", "", content).strip()
        if not content:
            continue

        fields = {
            match.group(1).strip(): match.group(2).strip()
            for match in _FIELD_TOKEN_RE.finditer(content)
            if match.group(1).strip()
        }
        content_without_fields = _FIELD_TOKEN_RE.sub("", content)
        tags = tuple(
            dict.fromkeys(
                tag.strip()
                for tag in _TAG_TOKEN_RE.findall(content_without_fields)
                if tag.strip()
            )
        )
        title = _TAG_TOKEN_RE.sub("", content_without_fields).strip(" -|\t")
        if not title:
            # An empty outline line is editor/layout state, not a node.
            continue
        chunks: list[str] = []
        remaining = title
        while len(remaining) > 500:
            boundary = max(
                remaining.rfind("。", 0, 500),
                remaining.rfind("！", 0, 500),
                remaining.rfind("？", 0, 500),
                remaining.rfind(" ", 0, 500),
            )
            cut = boundary + 1 if boundary >= 200 else 500
            chunks.append(remaining[:cut].strip())
            remaining = remaining[cut:].strip()
        if remaining:
            chunks.append(remaining)
        for index, chunk in enumerate(chunks):
            lines.append(
                ParsedOutlineLine(
                    depth=depth,
                    title=chunk,
                    tags=tags if index == 0 else (),
                    fields=fields if index == 0 else {},
                )
            )
    return lines


def _notify_docs_node_changed(docs_library_id: uuid.UUID, node_id: uuid.UUID) -> None:
    """Best-effort hook to mark a Docs node for RAG re-indexing.

    Kept fully guarded: when the Docs RAG index is disabled (the default) or the
    optional dependency stack is missing, this must be a cheap no-op and must
    never raise, because it runs inside every Docs mutation transaction.
    """
    try:
        from ..rag.docs_index import enqueue_docs_reindex

        enqueue_docs_reindex(docs_library_id, node_id)
    except Exception:
        return


class DocsGraphService:
    """Operate on Docs nodes while preserving revisions and derived indexes."""

    async def _canonical_project_for_node(self, node_id: uuid.UUID) -> Project | None:
        """Resolve the active Project reverse-pointer without fail-open errors.

        Production callers always use SQLAlchemy ``AsyncSession``.  A handful
        of dependency-free legacy service doubles intentionally omit Project
        metadata; those are explicitly treated as an unsupported capability,
        not as a database failure.  Any exception from a real session is
        allowed to propagate so canonical roots cannot be mutated while the
        pointer check is unavailable.
        """

        if not isinstance(self.session, AsyncSession):
            return None
        result = await self.session.execute(
            select(Project)
            .where(
                Project.knowledge_node_id == node_id,
                Project.deleted_at.is_(None),
            )
            .limit(1)
        )
        return result.scalars().first()

    async def _ensure_parent_title_available(
        self,
        *,
        docs_library_id: uuid.UUID,
        parent: KnowledgeNode,
        title: str,
    ) -> None:
        """親と同名の子nodeになる作成・改名・移動を拒否する。

        判定対象は親node自身のtitleだけで、兄弟node同士の同名は許可する。
        （例: 同じ件名のメールを「メール管理」配下へ複数保存する場合）
        """
        title_identity = normalize_docs_title_identity(title)
        if not title_identity:
            return
        # Lock the parent row so a concurrent rename of the parent cannot slip
        # in between this read check and the insert/update.
        locked_parent = await self.session.execute(
            select(KnowledgeNode.title)
            .where(
                KnowledgeNode.id == parent.id,
                KnowledgeNode.docs_library_id == docs_library_id,
            )
            .with_for_update()
        )
        parent_title = locked_parent.scalar_one_or_none()
        if parent_title is None:
            parent_title = getattr(parent, "title", None)
        if normalize_docs_title_identity(parent_title) == title_identity:
            raise ValueError("親と同名の子nodeは作成できません")

    def __init__(
        self,
        session: AsyncSession,
        *,
        workspace_root: str | os.PathLike[str] | None = None,
    ):
        self.session = session
        # Docs から App library へ書き戻す処理（app_readme など）が使う実効 root。
        # ここを 1 本の正本にしておかないと、ロック取得側と実ファイル操作側で
        # root が食い違い、別ロックで同じファイルを触る事故になる。
        # ``None`` は app_storage の既定解決（AOITALK_WORKSPACES_DIR）に委ねる。
        self.workspace_root = workspace_root

    async def ensure_library(self, user_id: uuid.UUID | None):
        return await ensure_docs_library(self.session, owner_user_id=user_id)

    async def ensure_project_information_library(
        self,
        project_id: uuid.UUID,
        actor_user_id: uuid.UUID | None = None,
    ):
        """Resolve the owner's Personal Docs Library for Project information.

        Project identity lives on the canonical root/descendant nodes.  This
        helper only resolves that root's owner library; it never creates a
        project-scoped library row.
        """

        return await ensure_project_docs_library(
            self.session,
            project_id=project_id,
            actor_user_id=actor_user_id,
        )

    async def get_project_information_library(
        self,
        project_id: uuid.UUID,
        actor_user_id: uuid.UUID | None = None,
    ):
        """Read the owner's Personal Docs Library for Project information."""

        return await get_project_docs_library(
            self.session,
            project_id=project_id,
            actor_user_id=actor_user_id,
        )

    async def _ensure_write_access(
        self,
        node: KnowledgeNode | None,
        user_id: uuid.UUID | None,
        *,
        include_archived: bool = True,
        project_id: uuid.UUID | None = None,
    ) -> None:
        """Re-check Docs write ACL in the transaction performing a mutation."""

        if user_id is None:
            return
        if node is None:
            return
        # Lightweight service doubles from the legacy direct-tool boundary
        # carry only ``id``/parent fields.  Persisted KnowledgeNode rows
        # always expose ``docs_library_id``; skip ACL lookup only for those
        # deliberately unscoped doubles so a missing ``session.get`` cannot
        # turn a compatibility test into a production bypass.
        if not hasattr(node, "docs_library_id") and not hasattr(node, "workspace_id"):
            return
        # Unified project roots are children of the owner's Personal hub. A
        # project writer may create/edit that child even though the hub itself
        # is owner-private; the project ACL remains authoritative and the
        # parent must be either the hub or an existing node in the same project.
        if project_id is not None:
            parent_project_id = _coerce_uuid(getattr(node, "project_id", None))
            parent_system_key = str(getattr(node, "system_key", "") or "")
            if parent_project_id in (None, project_id) and (
                parent_project_id == project_id
                or parent_system_key == "project_information_root"
            ) and await ProjectRepository.has_permission(
                self.session,
                project_id=project_id,
                user_id=user_id,
                permission="write",
            ):
                return
        if not await can_write_node(
            self.session,
            node,
            user_id,
            include_archived=include_archived,
        ):
            raise PermissionError("Docs nodeへの書き込み権限がありません")

    async def resolve_node(
        self,
        *,
        docs_library_id: uuid.UUID | None = None,
        workspace_id: uuid.UUID | None = None,
        ref: str = "",
        project_id: uuid.UUID | None = None,
        allow_archived: bool = False,
        user_id: uuid.UUID | None = None,
        required: str = "read",
    ) -> KnowledgeNode:
        # A fully-qualified node UUID can be authorized directly from the
        # node's own Personal Library/Project ACL.  Read-only general Docs
        # scope therefore may omit a library discriminator; title/prefix and
        # materializing aliases still require the canonical library id.
        resolved_docs_library_id = (
            _resolve_docs_library_id(docs_library_id, workspace_id)
            if docs_library_id is not None or workspace_id is not None
            else None
        )
        text = str(ref or "").strip()
        if not text:
            raise ValueError("node reference is required")

        parsed_uuid = _coerce_uuid(text)
        if resolved_docs_library_id is None and parsed_uuid is None:
            raise ValueError("docs_library_id is required for non-UUID node references")

        if text.casefold() == "today":
            if resolved_docs_library_id is None:
                raise ValueError("docs_library_id is required for today")
            if required == "write":
                if user_id is None:
                    raise PermissionError("Docs nodeへの書き込み権限がありません")
                node, _, _ = await self.ensure_daily_page(
                    docs_library_id=resolved_docs_library_id,
                    user_id=user_id,
                    day=date.today(),
                )
            else:
                # Read/search paths must not materialize a missing Daily page
                # (or seed the Day supertag).  Resolve only the already
                # persisted node and fail closed when it does not exist.
                today_conditions: list[Any] = [
                    KnowledgeNode.docs_library_id == resolved_docs_library_id,
                    KnowledgeNode.day_date == date.today(),
                ]
                if project_id is not None:
                    today_conditions.append(KnowledgeNode.project_id == project_id)
                if not allow_archived:
                    today_conditions.append(KnowledgeNode.archived_at.is_(None))
                today_result = await self.session.execute(
                    select(KnowledgeNode)
                    .where(*today_conditions)
                    .order_by(KnowledgeNode.created_at)
                    .limit(1)
                )
                node = today_result.scalar_one_or_none()
                if node is None:
                    raise ValueError("node not found: today")
            if user_id is not None and not await can_read_node(
                self.session,
                node,
                user_id,
                required=required,
                include_archived=allow_archived,
            ):
                raise ValueError("node not found: today")
            return node

        # UUID として解釈できる参照は正規化して直接解決する。
        # （クライアント生成 ID がハイフン位置の異なる 32hex で届いても、
        #   uuid.UUID() の寛容パースにより create 時と同じ正規形へ揃う）
        if parsed_uuid is not None:
            node = await self.session.get(KnowledgeNode, parsed_uuid)
            if (
                node
                and (
                    resolved_docs_library_id is None
                    or node.docs_library_id == resolved_docs_library_id
                )
                and (project_id is None or node.project_id == project_id)
                and (allow_archived or node.archived_at is None)
            ):
                if user_id is None or await can_read_node(
                    self.session,
                    node,
                    user_id,
                    required=required,
                    include_archived=allow_archived,
                ):
                    return node
                raise ValueError(f"node not found: {text}")
            if _UUID_RE.match(text):
                raise ValueError(f"node not found: {text}")
            # 非正規形はタイトル一致などのフォールバックに委ねる

        if re.fullmatch(r"[0-9a-fA-F]{8,32}", text):
            if resolved_docs_library_id is None:
                raise ValueError("docs_library_id is required for node prefixes")
            id_text = (
                func.replace(cast(KnowledgeNode.id, String), "-", "")
                if "-" not in text
                else cast(KnowledgeNode.id, String)
            )
            conditions = [
                KnowledgeNode.docs_library_id == resolved_docs_library_id,
                id_text.ilike(f"{text}%"),
            ]
            if project_id is not None:
                conditions.append(KnowledgeNode.project_id == project_id)
            if not allow_archived:
                conditions.append(KnowledgeNode.archived_at.is_(None))
            result = await self.session.execute(
                select(KnowledgeNode).where(*conditions)
            )
            matches = list(result.scalars().all())
            if len(matches) == 1:
                candidate = matches[0]
                if user_id is None or await can_read_node(
                    self.session,
                    candidate,
                    user_id,
                    required=required,
                    include_archived=allow_archived,
                ):
                    return candidate
                raise ValueError(f"node not found: {text}")
            if matches:
                raise ValueError(f"node prefix is ambiguous: {text}")

        conditions: list[Any] = [
            KnowledgeNode.docs_library_id == resolved_docs_library_id,
            KnowledgeNode.title == text,
        ]
        if not allow_archived:
            conditions.append(KnowledgeNode.archived_at.is_(None))
        if project_id is not None:
            conditions.append(KnowledgeNode.project_id == project_id)
        result = await self.session.execute(
            select(KnowledgeNode).where(*conditions).order_by(KnowledgeNode.updated_at.desc()).limit(2)
        )
        matches = list(result.scalars().all())
        if len(matches) == 1:
            candidate = matches[0]
            if user_id is None or await can_read_node(
                self.session,
                candidate,
                user_id,
                required=required,
                include_archived=allow_archived,
            ):
                return candidate
            raise ValueError(f"node not found: {text}")
        if len(matches) > 1:
            raise ValueError(f"node reference is ambiguous: {text}")
        raise ValueError(f"node not found: {text}")

    async def resolve_supertag(
        self,
        *,
        docs_library_id: uuid.UUID | None = None,
        workspace_id: uuid.UUID | None = None,
        tag: str = "",
        create: bool = True,
    ) -> KnowledgeSupertag:
        docs_library_id = _resolve_docs_library_id(docs_library_id, workspace_id)
        text = str(tag or "").strip().lstrip("#")
        if not text:
            raise ValueError("tag is required")
        parsed_uuid = _coerce_uuid(text)
        if parsed_uuid is not None:
            row = await self.session.get(KnowledgeSupertag, parsed_uuid)
            if row and row.docs_library_id == docs_library_id:
                return row
            raise ValueError(f"supertag not found: {tag}")

        result = await self.session.execute(
            select(KnowledgeSupertag)
            .where(
                KnowledgeSupertag.docs_library_id == docs_library_id,
                or_(
                    KnowledgeSupertag.system_key == text.casefold(),
                    func.lower(KnowledgeSupertag.name) == text.casefold(),
                ),
            )
            .order_by(KnowledgeSupertag.created_at)
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if row is not None:
            return row
        if not create:
            raise ValueError(f"supertag not found: {tag}")
        row = KnowledgeSupertag(
            docs_library_id=docs_library_id,
            name=text[:120],
            base_type="note",
            color="#64748b",
            template_json={},
            pinned_field_ids=[],
            config_json={},
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def resolve_project(self, project_ref: str = "") -> Project | None:
        text = str(project_ref or "").strip()
        if not text:
            return None
        parsed_uuid = _coerce_uuid(text)
        conditions = [Project.deleted_at.is_(None)]
        if parsed_uuid is not None:
            conditions.append(Project.id == parsed_uuid)
        else:
            conditions.append(
                or_(
                    func.lower(Project.slug) == text.casefold(),
                    func.lower(Project.name) == text.casefold(),
                    Project.name.ilike(f"%{text}%"),
                )
            )
        result = await self.session.execute(select(Project).where(*conditions).limit(2))
        matches = list(result.scalars().all())
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(f"project reference is ambiguous: {project_ref}")
        return None

    async def upsert_search_index(self, node: KnowledgeNode) -> None:
        row = await self.session.get(KnowledgeSearchIndex, node.id)
        if row is None:
            row = KnowledgeSearchIndex(node_id=node.id)
            self.session.add(row)
        row.docs_library_id = node.docs_library_id
        row.project_id = node.project_id
        row.title_text = node.title or ""
        row.body_text_plain = docs_searchable_body_text(
            node.body_text,
            node.body_json,
        )
        row.updated_at = _now()

    async def sync_reference_edges(self, node: KnowledgeNode, user_id: uuid.UUID | None) -> None:
        await self.session.execute(
            delete(KnowledgeEdge).where(
                KnowledgeEdge.source_node_id == node.id,
                KnowledgeEdge.relation_type.in_(["inline_ref", "references"]),
            )
        )
        text = "\n".join([node.title or "", node.body_text or ""])
        target_ids: list[uuid.UUID] = []
        for match in _NODE_TOKEN_RE.finditer(text):
            value = match.group(1) or match.group(2)
            parsed = _coerce_uuid(value)
            if parsed is not None and parsed != node.id and parsed not in target_ids:
                target_ids.append(parsed)
        if not target_ids:
            return
        existing_result = await self.session.execute(
            select(KnowledgeNode.id).where(
                KnowledgeNode.docs_library_id == node.docs_library_id,
                KnowledgeNode.id.in_(target_ids),
                KnowledgeNode.archived_at.is_(None),
            )
        )
        existing_ids = set(existing_result.scalars().all())
        for target_id in target_ids:
            if target_id not in existing_ids:
                continue
            self.session.add(
                KnowledgeEdge(
                    source_node_id=node.id,
                    target_node_id=target_id,
                    relation_type="inline_ref",
                    confidence=1,
                    created_by=user_id,
                )
            )

    async def record_node_change(
        self,
        node: KnowledgeNode,
        user_id: uuid.UUID | None,
        change_summary: str,
        source_refs: list[dict[str, Any]] | None = None,
    ) -> None:
        await self.upsert_search_index(node)
        await self.sync_reference_edges(node, user_id)
        self.session.add(
            KnowledgeRevision(
                node_id=node.id,
                title=node.title or "",
                body_json=node.body_json or {},
                body_text=node.body_text or "",
                change_summary=change_summary,
                source_refs_json=source_refs or [],
                created_by=user_id,
            )
        )
        _notify_docs_node_changed(node.docs_library_id, node.id)

    async def _next_sort_order(self, parent_id: uuid.UUID | None, docs_library_id: uuid.UUID) -> float:
        result = await self.session.execute(
            select(func.max(KnowledgeNode.sort_order)).where(
                KnowledgeNode.docs_library_id == docs_library_id,
                KnowledgeNode.parent_id == parent_id,
            )
        )
        current = result.scalar_one_or_none()
        return float(current or 0) + 1

    async def first_sort_order(self, parent_id: uuid.UUID | None, docs_library_id: uuid.UUID) -> float:
        """既存の先頭より前へ差し込む sort_order。子が無ければ末尾採番と同じ値になる。"""
        result = await self.session.execute(
            select(func.min(KnowledgeNode.sort_order)).where(
                KnowledgeNode.docs_library_id == docs_library_id,
                KnowledgeNode.parent_id == parent_id,
                KnowledgeNode.archived_at.is_(None),
            )
        )
        current = result.scalar_one_or_none()
        return 1.0 if current is None else float(current) - 1

    async def create_node(
        self,
        *,
        docs_library_id: uuid.UUID | None = None,
        workspace_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
        title: str = "",
        parent: KnowledgeNode | None = None,
        project_id: uuid.UUID | None = None,
        body_text: str = "",
        body_json: dict[str, Any] | None = None,
        node_type: str = "node",
        sort_order: float | None = None,
        node_id: uuid.UUID | None = None,
        system_key: str | None = None,
        day_date: date | None = None,
        source_refs: list[dict[str, Any]] | None = None,
    ) -> KnowledgeNode:
        docs_library_id = _resolve_docs_library_id(docs_library_id, workspace_id)
        if parent is not None and parent.docs_library_id != docs_library_id:
            raise ValueError("親nodeと作成先workspaceが一致しません")
        # Descendants inherit the canonical Project scope from their parent.
        # Resolve it before ACL enforcement so a writer does not need to send
        # a redundant project_id for every child create.
        if project_id is None and parent is not None:
            project_id = _coerce_uuid(getattr(parent, "project_id", None))
        if user_id is not None:
            if parent is not None:
                await self._ensure_write_access(parent, user_id, project_id=project_id)
            else:
                library = await self.session.get(DocsLibrary, docs_library_id)
                if library is None or not await library_can_write(
                    self.session, library, user_id
                ):
                    raise PermissionError("Docs workspaceへの書き込み権限がありません")
        if project_id is not None and parent is None:
            raise ValueError(
                "Project-scoped Docs nodes require a parent under 案件情報"
            )
        root_page_id = None
        if parent is not None:
            root_page_id = parent.root_page_id or parent.id
        clean_title = (title or "").strip()[:500]
        normalized_body_json = (
            dict(body_json) if isinstance(body_json, dict) else {}
        )
        explicit_blank = is_explicit_blank_paragraph(
            clean_title,
            normalized_body_json,
            node_type,
        )
        if not clean_title and not explicit_blank:
            raise ValueError("空行はDocs nodeとして保存できません")
        if clean_title and normalized_body_json.get("blank") is True:
            # ``blank`` is a discriminator, not arbitrary user metadata.  A
            # meaningful title must never be persisted with a stale marker.
            normalized_body_json = clear_blank_paragraph_marker(normalized_body_json)
        if parent is not None and clean_title:
            await self._ensure_parent_title_available(
                docs_library_id=docs_library_id,
                parent=parent,
                title=clean_title,
            )
        # 不変条件(1.6a): 本文は子node階層が正本。body_text は常にtitle mirror。
        # Python organizer経路だけ任意本文を許す例外を残すと、Web/モバイルとの
        # 往復で巨大title・二重正本が再発するため、非mirror値は明示的に拒否する。
        body_text_value = "" if explicit_blank else _title_mirror(clean_title)
        if explicit_blank:
            # The canonical representation has an empty body mirror as well
            # as an empty title.  Do not allow a caller to smuggle a second
            # body value through the legacy body_text argument.
            if body_text not in (None, ""):
                raise ValueError("空paragraphのbody_textは空である必要があります")
        elif str(body_text or "").strip() not in {"", body_text_value}:
            raise ValueError("Docs body content must be represented by child nodes")
        node = KnowledgeNode(
            id=node_id if node_id is not None else uuid.uuid4(),
            docs_library_id=docs_library_id,
            parent_id=parent.id if parent else None,
            root_page_id=root_page_id,
            project_id=project_id,
            system_key=system_key,
            title=clean_title,
            body_text=body_text_value,
            body_json=normalized_body_json,
            node_type=node_type,
            day_date=day_date,
            sort_order=sort_order
            if sort_order is not None
            else await self._next_sort_order(parent.id if parent else None, docs_library_id),
            created_by=user_id,
            updated_by=user_id,
        )
        self.session.add(node)
        await self.session.flush()
        await self.record_node_change(node, user_id, "nodeを作成", source_refs)
        await self.session.flush()
        return node

    async def ensure_system_node(
        self,
        *,
        docs_library_id: uuid.UUID | None = None,
        workspace_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
        title: str = "",
        parent: KnowledgeNode,
        project_id: uuid.UUID | None,
        system_key: str,
        body_json: dict[str, Any] | None = None,
        source_refs: list[dict[str, Any]] | None = None,
    ) -> tuple[KnowledgeNode, bool]:
        """Create or repair one idempotent system node.

        ``docs_library_id + system_key`` is the stable identity. This is used by
        direct tools whose retries must not create duplicate Docs children.
        """

        docs_library_id = _resolve_docs_library_id(docs_library_id, workspace_id)

        if user_id is None:
            raise PermissionError("Docs nodeへの書き込み権限がありません")
        # Legacy in-process callers sometimes pass a lightweight parent object
        # with no scope attribute.  The real ORM node always has
        # ``docs_library_id``; fallback to the explicit method scope only for
        # those test/mobile doubles (never for a persisted row).
        parent_library_id = getattr(
            parent,
            "docs_library_id",
            getattr(parent, "workspace_id", docs_library_id),
        )
        if parent_library_id != docs_library_id:
            raise ValueError("親nodeと作成先workspaceが一致しません")
        await self._ensure_write_access(parent, user_id, project_id=project_id)

        async def _find() -> KnowledgeNode | None:
            result = await self.session.execute(
                select(KnowledgeNode).where(
                    KnowledgeNode.docs_library_id == docs_library_id,
                    KnowledgeNode.system_key == system_key,
                )
            )
            return result.scalar_one_or_none()

        existing = await _find()
        if existing is None:
            try:
                async with self.session.begin_nested():
                    node = await self.create_node(
                        docs_library_id=docs_library_id,
                        user_id=user_id,
                        title=title,
                        parent=parent,
                        project_id=project_id,
                        system_key=system_key,
                        body_json=body_json or {},
                        source_refs=source_refs,
                    )
                return node, True
            except IntegrityError:
                existing = await _find()
                if existing is None:
                    raise

        # Deterministic retries may find an existing row created by another
        # actor.  Reparenting/repairing it is still a mutation and must honor
        # the nearest explicit share (a child-level read share downgrades a
        # writable ancestor).
        await self._ensure_write_access(
            existing,
            user_id,
            project_id=_coerce_uuid(getattr(existing, "project_id", None)),
        )
        changed = False
        expected_root_id = parent.root_page_id or parent.id
        if existing.parent_id != parent.id or existing.root_page_id != expected_root_id:
            existing.parent_id = parent.id
            existing.root_page_id = expected_root_id
            changed = True
        if existing.project_id != project_id:
            existing.project_id = project_id
            changed = True
        clean_title = str(title or "Untitled").strip()[:500]
        if existing.title != clean_title:
            existing.title = clean_title
            existing.body_text = clean_title
            changed = True
        expected_body_json = body_json or {}
        if existing.body_json != expected_body_json:
            existing.body_json = expected_body_json
            changed = True
        if existing.archived_at is not None:
            existing.archived_at = None
            changed = True
        if changed:
            existing.updated_by = user_id
            await self.record_node_change(
                existing,
                user_id,
                "system nodeを冪等更新",
                source_refs,
            )
            await self.session.flush()
        return existing, False

    async def update_node(
        self,
        *,
        node: KnowledgeNode,
        user_id: uuid.UUID | None,
        title: str | None = None,
        description: str | None = None,
        body_json: dict[str, Any] | None = None,
        body_text: str | None = None,
        source_refs: list[dict[str, Any]] | None = None,
        change_summary: str = "nodeを更新",
    ) -> KnowledgeNode:
        await self._ensure_write_access(
            node,
            user_id,
            project_id=_coerce_uuid(getattr(node, "project_id", None)),
        )
        # ``body_json`` is the same-request discriminator for a persisted
        # blank paragraph.  Existing metadata is copied before we mutate it
        # so a nonblank transition removes only ``blank`` and never loses
        # provenance/display fields.
        supplied_body_json = isinstance(body_json, dict)
        current_body_json = (
            dict(node.body_json) if isinstance(node.body_json, dict) else {}
        )
        next_title = node.title
        explicit_blank = False
        if title is not None:
            next_title = title.strip()[:500]
            explicit_blank = is_explicit_blank_paragraph(
                next_title,
                body_json,
                getattr(node, "node_type", "node"),
            )
            if not next_title and not explicit_blank:
                raise ValueError("空行はDocs nodeとして保存できません")
        elif not next_title and supplied_body_json:
            # A metadata-only update of an already blank paragraph must carry
            # the marker in that same request as well; otherwise it would
            # silently turn the row into an invalid legacy blank.
            explicit_blank = is_explicit_blank_paragraph(
                next_title,
                body_json,
                getattr(node, "node_type", "node"),
            )
            if not explicit_blank:
                raise ValueError("空paragraphの更新にはblank markerが必要です")

        if title is not None and next_title:
            if node.parent_id is not None:
                parent = await self.session.get(KnowledgeNode, node.parent_id)
                if parent is None:
                    raise ValueError("親nodeが見つかりません")
                await self._ensure_parent_title_available(
                    docs_library_id=node.docs_library_id,
                    parent=parent,
                    title=next_title,
                )
            node.title = next_title
            # 不変条件(1.6a): title 変更のたび body_text ミラーを再計算する。
            node.body_text = _title_mirror(node.title)
            await self._sync_bound_task_title(node=node, user_id=user_id)
        elif title is not None and explicit_blank:
            node.title = ""
            node.body_text = ""
        elif title is None and explicit_blank:
            node.title = ""
            node.body_text = ""
        if description is not None:
            node.description = str(description)[:200000]
        if supplied_body_json:
            if explicit_blank:
                node.body_json = blank_paragraph_body_json(body_json)
            elif next_title:
                # A nonblank title is authoritative.  If the client sent a
                # stale blank marker, clear only that marker while retaining
                # all other body metadata.
                node.body_json = clear_blank_paragraph_marker(body_json)
            else:
                # An existing blank row can only remain blank when the same
                # request carries the canonical marker.
                raise ValueError("空paragraphの更新にはblank markerが必要です")
        elif title is not None and next_title:
            if current_body_json.get("blank") is True:
                node.body_json = clear_blank_paragraph_marker(current_body_json)
        elif title is None and not next_title and current_body_json.get("blank") is not True:
            # A legacy malformed blank row must not be silently made valid by
            # an unrelated metadata/description update.
            raise ValueError("空行はDocs nodeとして保存できません")
        if body_text is not None:
            if not node.title:
                if body_text not in (None, ""):
                    raise ValueError("空paragraphのbody_textは空である必要があります")
                node.body_text = ""
            else:
                requested = str(body_text).strip()
                mirror = _title_mirror(node.title)
                if requested not in {"", mirror}:
                    raise ValueError("Docs body content must be represented by child nodes")
                node.body_text = mirror
        node.updated_by = user_id
        node.updated_at = _now()
        await self.record_node_change(node, user_id, change_summary, source_refs)
        await self.session.flush()
        return node

    async def add_tag(
        self,
        *,
        node: KnowledgeNode,
        tag: KnowledgeSupertag,
        user_id: uuid.UUID | None,
    ) -> bool:
        if tag.name.strip() == "倉庫":
            locked_result = await self.session.execute(
                select(KnowledgeNode).where(
                    KnowledgeNode.id == node.id,
                    KnowledgeNode.docs_library_id == node.docs_library_id,
                ).with_for_update()
            )
            locked_node = locked_result.scalar_one_or_none()
            if locked_node is None:
                raise ValueError("Docs node not found")
            if await is_film_docs_node(self.session, locked_node):
                raise ValueError("Film配下へ倉庫Supertagは付けられません")
            node = locked_node
        await self._ensure_write_access(
            node,
            user_id,
            project_id=_coerce_uuid(getattr(node, "project_id", None)),
        )
        link = await self.session.get(
            KnowledgeNodeSupertag,
            {"node_id": node.id, "supertag_id": tag.id},
        )
        if link is not None:
            return False
        self.session.add(
            KnowledgeNodeSupertag(node_id=node.id, supertag_id=tag.id, created_by=user_id)
        )
        await self.session.flush()
        if tag.system_key == SYSTEM_TASK_TAG:
            await self._ensure_bound_task(node=node, user_id=user_id)
        return True

    async def remove_tag(
        self,
        *,
        node: KnowledgeNode,
        tag: KnowledgeSupertag,
        user_id: uuid.UUID | None,
    ) -> bool:
        await self._ensure_write_access(
            node,
            user_id,
            project_id=_coerce_uuid(getattr(node, "project_id", None)),
        )
        link = await self.session.get(
            KnowledgeNodeSupertag,
            {"node_id": node.id, "supertag_id": tag.id},
        )
        if link is None:
            return False
        await self.session.delete(link)
        await self.session.flush()
        if tag.system_key == SYSTEM_TASK_TAG:
            await self._unlink_bound_task(node=node, user_id=user_id)
        return True

    async def _ensure_bound_task(self, *, node: KnowledgeNode, user_id: uuid.UUID | None) -> None:
        if user_id is None:
            return
        existing = await self.session.execute(
            select(Task.id).where(Task.knowledge_node_id == node.id, Task.deleted_at.is_(None)).limit(1)
        )
        if existing.scalar_one_or_none() is not None:
            return
        await TaskManagementService().create_task(
            self.session,
            user_id=user_id,
            project_id=node.project_id,
            knowledge_node_id=node.id,
            title=node.title or "Untitled",
            description=node.description or None,
            source="docs",
            status="todo",
            priority="medium",
            task_metadata={"source": "docs", "knowledge_node_id": str(node.id)},
            commit=False,
        )

    async def _unlink_bound_task(self, *, node: KnowledgeNode, user_id: uuid.UUID | None) -> None:
        if user_id is None:
            return
        result = await self.session.execute(
            select(Task).where(Task.knowledge_node_id == node.id, Task.deleted_at.is_(None)).limit(1)
        )
        task = result.scalar_one_or_none()
        if task is None:
            return
        await TaskManagementService().update_task(
            self.session,
            user_id=user_id,
            task_id=task.id,
            updates={"knowledge_node_id": None},
            commit=False,
        )

    async def _sync_bound_task_title(self, *, node: KnowledgeNode, user_id: uuid.UUID | None) -> None:
        if user_id is None:
            return
        result = await self.session.execute(
            select(Task).where(Task.knowledge_node_id == node.id, Task.deleted_at.is_(None)).limit(1)
        )
        task = result.scalar_one_or_none()
        if task is None or task.title == node.title:
            return
        await TaskManagementService().update_task(
            self.session,
            user_id=user_id,
            task_id=task.id,
            updates={"title": node.title},
            commit=False,
        )

    async def resolve_node_fields(self, node: KnowledgeNode) -> dict[str, KnowledgeField]:
        tag_result = await self.session.execute(
            select(KnowledgeSupertag.id)
            .join(KnowledgeNodeSupertag, KnowledgeNodeSupertag.supertag_id == KnowledgeSupertag.id)
            .where(
                KnowledgeNodeSupertag.node_id == node.id,
                KnowledgeSupertag.docs_library_id == node.docs_library_id,
            )
        )
        tag_ids = list(tag_result.scalars().all())
        if not tag_ids:
            return {}
        field_result = await self.session.execute(
            select(KnowledgeField)
            .where(
                KnowledgeField.supertag_id.in_(tag_ids),
                KnowledgeField.docs_library_id == node.docs_library_id,
            )
            .order_by(KnowledgeField.sort_order, KnowledgeField.created_at)
        )
        fields: dict[str, KnowledgeField] = {}
        for field in field_result.scalars().all():
            fields[field.name.casefold()] = field
            if field.system_key:
                fields[field.system_key.casefold()] = field
        return fields

    async def set_fields(
        self,
        *,
        node: KnowledgeNode,
        values: dict[str, Any],
        user_id: uuid.UUID | None,
    ) -> dict[str, str]:
        await self._ensure_write_access(
            node,
            user_id,
            project_id=_coerce_uuid(getattr(node, "project_id", None)),
        )
        fields_by_ref = await self.resolve_node_fields(node)
        updated: dict[str, str] = {}
        task_updates: dict[str, Any] = {}
        for field_ref, raw_value in values.items():
            field = fields_by_ref.get(str(field_ref).casefold())
            if field is None:
                raise ValueError(f"field not found on node tags: {field_ref}")
            if field.system_key in TASK_FIELD_TO_TASK_UPDATE:
                task_updates[TASK_FIELD_TO_TASK_UPDATE[field.system_key]] = self._coerce_task_field_value(
                    field.system_key,
                    raw_value,
                )
                updated[field.name] = "task"
                continue
            await self._set_field_value(node=node, field=field, raw_value=raw_value, user_id=user_id)
            updated[field.name] = "docs"
        if task_updates:
            await self._update_bound_task(node=node, user_id=user_id, updates=task_updates)
        await self.session.flush()
        return updated

    def _coerce_task_field_value(self, system_key: str, raw_value: Any) -> Any:
        if raw_value in ("", None):
            return None
        if system_key in {"task_due", "task_start"}:
            return _parse_datetime(raw_value)
        if system_key == "task_project":
            return _coerce_uuid(raw_value)
        return str(raw_value)

    async def _update_bound_task(
        self,
        *,
        node: KnowledgeNode,
        user_id: uuid.UUID | None,
        updates: dict[str, Any],
    ) -> None:
        if user_id is None:
            raise ValueError("user_id is required for task field updates")
        result = await self.session.execute(
            select(Task).where(Task.knowledge_node_id == node.id, Task.deleted_at.is_(None)).limit(1)
        )
        task = result.scalar_one_or_none()
        if task is None:
            raise ValueError("node is not bound to a task")
        await TaskManagementService().update_task(
            self.session,
            user_id=user_id,
            task_id=task.id,
            updates=updates,
            commit=False,
        )

    async def _set_field_value(
        self,
        *,
        node: KnowledgeNode,
        field: KnowledgeField,
        raw_value: Any,
        user_id: uuid.UUID | None,
    ) -> None:
        row = await self.session.get(
            KnowledgeFieldValue,
            {"node_id": node.id, "field_id": field.id},
        )
        if row is None:
            row = KnowledgeFieldValue(node_id=node.id, field_id=field.id)
            self.session.add(row)
        row.value_json = None
        row.value_text = None
        row.value_number = None
        row.value_datetime = None
        row.target_node_id = None
        if raw_value in ("", None):
            await self.session.delete(row)
            return
        field_type = str(field.field_type or "text")
        if field_type == "number":
            row.value_number = float(raw_value)
        elif field_type == "date":
            row.value_datetime = _parse_datetime(raw_value)
        elif field_type == "checkbox":
            row.value_json = {"value": bool(raw_value)}
        elif field_type == "reference":
            target = await self.resolve_node(
                docs_library_id=node.docs_library_id,
                ref=str(raw_value),
                user_id=user_id,
                required="read",
            )
            row.target_node_id = target.id
        else:
            row.value_text = str(raw_value)
        row.updated_by = user_id
        row.updated_at = _now()

    async def create_nodes_from_outline(
        self,
        *,
        docs_library_id: uuid.UUID | None = None,
        workspace_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
        parent: KnowledgeNode,
        outline_text: str,
        project_id: uuid.UUID | None = None,
    ) -> list[KnowledgeNode]:
        docs_library_id = _resolve_docs_library_id(docs_library_id, workspace_id)
        parsed_lines = _parse_outline_text(outline_text)
        stack: dict[int, KnowledgeNode] = {-1: parent}
        created: list[KnowledgeNode] = []
        for parsed in parsed_lines:
            parent_depth = parsed.depth - 1
            while parent_depth not in stack and parent_depth >= -1:
                parent_depth -= 1
            parent_node = stack.get(parent_depth, parent)
            if normalize_docs_title_identity(parsed.title) == normalize_docs_title_identity(parent_node.title):
                # Markdown/OneNote exports commonly repeat the section label
                # as the first paragraph.  Reuse the existing parent so that
                # following indented lines remain attached to the real node.
                node = parent_node
            else:
                node = await self.create_node(
                    docs_library_id=docs_library_id,
                    user_id=user_id,
                    parent=parent_node,
                    project_id=project_id or parent.project_id,
                    title=parsed.title,
                )
                created.append(node)
            stack[parsed.depth] = node
            for deeper in [depth for depth in stack if depth > parsed.depth]:
                stack.pop(deeper, None)
            for tag_name in parsed.tags:
                tag = await self.resolve_supertag(docs_library_id=docs_library_id, tag=tag_name, create=True)
                await self.add_tag(node=node, tag=tag, user_id=user_id)
            if parsed.fields:
                await self.set_fields(node=node, values=parsed.fields, user_id=user_id)
        return created

    async def move_node(
        self,
        *,
        node: KnowledgeNode,
        new_parent: KnowledgeNode,
        user_id: uuid.UUID | None,
        leave_reference: bool = False,
    ) -> KnowledgeNode:
        node_library_id = _coerce_uuid(getattr(node, "docs_library_id", None))
        parent_library_id = _coerce_uuid(getattr(new_parent, "docs_library_id", None))
        if (
            node_library_id is not None
            and parent_library_id is not None
            and node_library_id != parent_library_id
        ):
            raise ValueError("異なるDocs Library間でnodeを移動できません")

        node_project_id = _coerce_uuid(getattr(node, "project_id", None))
        parent_project_id = _coerce_uuid(getattr(new_parent, "project_id", None))
        if node_project_id != parent_project_id:
            # A project node may be attached to its owner's Personal hub
            # (the hub itself has no project_id), but an ordinary Personal
            # node must never be promoted into a Project subtree and a node
            # must never cross from one Project into another.
            is_project_hub = (
                parent_project_id is None
                and str(getattr(new_parent, "system_key", "") or "")
                == "project_information_root"
                and getattr(new_parent, "parent_id", None) is None
                and str(getattr(node, "system_key", "") or "")
                == f"project_information:{node_project_id}"
            )
            if not (node_project_id is not None and is_project_hub):
                raise ValueError("異なるProject間でnodeを移動できません")

        # ``Project.knowledge_node_id`` is the canonical project-information
        # root pointer.  Moving that node under an arbitrary same-project
        # parent would silently destroy the canonical hierarchy; only the
        # validated owner Personal hub is an allowed destination.
        canonical_project = await self._canonical_project_for_node(node.id)
        if canonical_project is not None:
            hub_ok = (
                parent_project_id is None
                and str(getattr(new_parent, "system_key", "") or "")
                == "project_information_root"
                and getattr(new_parent, "parent_id", None) is None
                and _coerce_uuid(getattr(new_parent, "docs_library_id", None))
                == node_library_id
            )
            if hub_ok:
                library = await self.session.get(DocsLibrary, new_parent.docs_library_id)
                hub_ok = bool(
                    library is not None
                    and str(getattr(library, "library_type", "personal") or "personal").lower()
                    == "personal"
                    and _coerce_uuid(getattr(library, "owner_user_id", None))
                    == _coerce_uuid(getattr(canonical_project, "owner_id", None))
                    and _coerce_uuid(getattr(new_parent, "root_page_id", None))
                    == _coerce_uuid(getattr(new_parent, "id", None))
                )
            if not hub_ok:
                raise ValueError("案件情報の正本rootはPersonal hub以外へ移動できません")

        await self._ensure_write_access(
            node,
            user_id,
            project_id=node_project_id,
        )
        await self._ensure_write_access(
            new_parent,
            user_id,
            project_id=parent_project_id or node_project_id,
        )
        old_parent_id = node.parent_id
        if node.id == new_parent.id:
            raise ValueError("node cannot be moved under itself")
        await self._ensure_parent_title_available(
            docs_library_id=node.docs_library_id,
            parent=new_parent,
            title=node.title,
        )
        # 循環防止: new_parent が node のサブツリー内なら拒否する。
        # new_parent から親を根まで遡り、node.id に当たれば子孫への移動＝循環。
        ancestor = new_parent
        seen: set[uuid.UUID] = set()
        for _ in range(513):
            if ancestor is not None and node_library_id is not None:
                ancestor_library_id = _coerce_uuid(
                    getattr(ancestor, "docs_library_id", None)
                )
                if ancestor_library_id != node_library_id:
                    raise ValueError("異なるDocs Libraryの親階層は辿れません")
            if ancestor is None:
                break
            if ancestor.id == node.id:
                raise ValueError("node cannot be moved under its own descendant")
            if ancestor.id in seen or ancestor.parent_id is None:
                break
            seen.add(ancestor.id)
            ancestor = await self.session.get(KnowledgeNode, ancestor.parent_id)
        else:
            raise ValueError("node parent hierarchy is too deep or cyclic")
        node.parent_id = new_parent.id
        node.root_page_id = new_parent.root_page_id or new_parent.id
        # Preserve the node's Project identity when attaching its canonical
        # root to the Personal hub; otherwise both sides are same-project.
        node.project_id = parent_project_id or node_project_id
        node.sort_order = await self._next_sort_order(new_parent.id, node.docs_library_id)
        node.updated_by = user_id
        node.updated_at = _now()
        # 子孫の root_page_id を新しいルートページへ伝播する（不変条件: 検索index root_page 更新）。
        await self._propagate_root_page(node)
        if leave_reference and old_parent_id is not None:
            exists = await self.session.execute(
                select(KnowledgeNodePlacement.id)
                .where(
                    KnowledgeNodePlacement.node_id == node.id,
                    KnowledgeNodePlacement.parent_node_id == old_parent_id,
                )
                .limit(1)
            )
            if exists.scalar_one_or_none() is None:
                self.session.add(
                    KnowledgeNodePlacement(
                        node_id=node.id,
                        parent_node_id=old_parent_id,
                        sort_order=node.sort_order,
                        created_by=user_id,
                    )
                )
        await self.record_node_change(
            node,
            user_id,
            "nodeを参照を残して移動" if leave_reference else "nodeを移動",
        )
        await self.session.flush()
        return node

    async def _propagate_root_page(self, root_node: KnowledgeNode) -> None:
        """root_node 配下の全子孫の root_page_id を root_node のルートページへ揃える。"""
        new_root = root_node.root_page_id or root_node.id
        result = await self.session.execute(
            select(KnowledgeNode).where(KnowledgeNode.docs_library_id == root_node.docs_library_id)
        )
        children: dict[uuid.UUID | None, list[KnowledgeNode]] = {}
        for n in result.scalars().all():
            children.setdefault(n.parent_id, []).append(n)
        stack = list(children.get(root_node.id, []))
        seen: set[uuid.UUID] = {root_node.id}
        while stack:
            node = stack.pop()
            if node.id in seen:
                continue
            seen.add(node.id)
            node.root_page_id = new_root
            stack.extend(children.get(node.id, []))

    async def archive_node(self, *, node: KnowledgeNode, user_id: uuid.UUID | None) -> KnowledgeNode:
        if await self._canonical_project_for_node(node.id) is not None:
            raise ValueError("案件情報の正本rootはアーカイブできません")
        await self._ensure_write_access(
            node,
            user_id,
            project_id=_coerce_uuid(getattr(node, "project_id", None)),
        )
        node.archived_at = _now()
        node.updated_by = user_id
        node.updated_at = _now()
        await self.record_node_change(node, user_id, "nodeをアーカイブ")
        # アーカイブ時は連携タスクを unlink する（不変条件 1.6: nodes/archive → task unlink）。
        await self._unlink_bound_task(node=node, user_id=user_id)
        await self.session.flush()
        return node

    async def archive_subtree(
        self,
        *,
        root: KnowledgeNode,
        user_id: uuid.UUID | None,
    ) -> list[KnowledgeNode]:
        """root以下を全てarchiveし、activeな孤児・検索結果を残さない。"""
        if await self._canonical_project_for_node(root.id) is not None:
            raise ValueError("案件情報の正本rootはアーカイブできません")
        await self._ensure_write_access(
            root,
            user_id,
            project_id=_coerce_uuid(getattr(root, "project_id", None)),
        )
        result = await self.session.execute(
            select(KnowledgeNode).where(KnowledgeNode.docs_library_id == root.docs_library_id)
        )
        children: dict[uuid.UUID | None, list[KnowledgeNode]] = {}
        for node in result.scalars().all():
            children.setdefault(node.parent_id, []).append(node)
        ordered = [root]
        cursor = 0
        seen: set[uuid.UUID] = set()
        while cursor < len(ordered):
            node = ordered[cursor]
            cursor += 1
            if node.id in seen:
                continue
            seen.add(node.id)
            ordered.extend(children.get(node.id, []))
        archived: list[KnowledgeNode] = []
        for node in ordered:
            if node.archived_at is None:
                await self.archive_node(node=node, user_id=user_id)
                archived.append(node)
        return archived

    async def set_field_by_id(
        self,
        *,
        node: KnowledgeNode,
        field_id: uuid.UUID,
        value: Any,
        user_id: uuid.UUID | None,
    ) -> dict[str, str]:
        """push/REST の field_value 更新（field_id 直指定）。

        field を id で取得し、ノードのタグ定義に属することを検証したうえで
        ``{field.name: value}`` を組んで既存 ``set_fields`` に委譲する
        （task 系 system_key の連携タスク更新・型別格納をそのまま再利用）。
        """
        await self._ensure_write_access(
            node,
            user_id,
            project_id=_coerce_uuid(getattr(node, "project_id", None)),
        )
        field = await self.session.get(KnowledgeField, field_id)
        if field is None or field.docs_library_id != node.docs_library_id:
            raise ValueError(f"field not found: {field_id}")
        # set_fields が resolve_node_fields でノードのタグ定義に属すかを検証する。
        return await self.set_fields(node=node, values={field.name: value}, user_id=user_id)

    async def _ensure_system_node(
        self,
        *,
        docs_library_id: uuid.UUID,
        title: str,
        parent_id: uuid.UUID | None,
        sort_order: float,
        user_id: uuid.UUID | None,
        node_type: str = "system",
    ) -> KnowledgeNode:
        """Web `ensureSystemNode` 相当。title+parent で一意な祖先ノードを ensure する。"""
        if user_id is None:
            raise PermissionError("Docs nodeへの書き込み権限がありません")
        parent = (
            await self.session.get(KnowledgeNode, parent_id)
            if parent_id is not None
            else None
        )
        if parent_id is not None and parent is None:
            raise ValueError("親nodeが見つかりません")
        if parent is not None:
            if parent.docs_library_id != docs_library_id:
                raise ValueError("親nodeと作成先workspaceが一致しません")
            await self._ensure_write_access(
                parent,
                user_id,
                project_id=_coerce_uuid(getattr(parent, "project_id", None)),
            )
        conditions: list[Any] = [
            KnowledgeNode.docs_library_id == docs_library_id,
            KnowledgeNode.title == title,
            KnowledgeNode.archived_at.is_(None),
        ]
        if parent_id is None:
            conditions.append(KnowledgeNode.parent_id.is_(None))
        else:
            conditions.append(KnowledgeNode.parent_id == parent_id)
        result = await self.session.execute(
            select(KnowledgeNode).where(*conditions).order_by(KnowledgeNode.created_at).limit(1)
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            await self._ensure_write_access(
                existing,
                user_id,
                project_id=_coerce_uuid(getattr(existing, "project_id", None)),
            )
            return existing
        # Web は rootPageId=parentId（直近の親）を採用するため、それを踏襲する。
        node = KnowledgeNode(
            id=uuid.uuid4(),
            docs_library_id=docs_library_id,
            parent_id=parent_id,
            root_page_id=parent_id,
            title=title[:500],
            body_text=_title_mirror(title),
            body_json={"inline": [{"type": "text", "text": title}]},
            node_type=node_type,
            sort_order=sort_order,
            created_by=user_id,
            updated_by=user_id,
        )
        self.session.add(node)
        await self.session.flush()
        await self.record_node_change(node, user_id, "systemノードを作成")
        return node

    async def ensure_daily_page(
        self,
        *,
        docs_library_id: uuid.UUID,
        user_id: uuid.UUID | None,
        day: date,
    ) -> tuple[KnowledgeNode, KnowledgeSupertag, list[KnowledgeNodeSupertag]]:
        """Web `today/route.ts` と同一階層で Day ノードを ensure する。

        Daily notes > <year> > Week NN > Day の祖先を作成/正規化し、Day タグ付与と
        day_date 設定を行う。戻り値は (dayノード, Dayタグ, node_supertags)。
        """
        if user_id is None:
            raise PermissionError("Docs workspaceへの書き込み権限がありません")
        library = await self.session.get(DocsLibrary, docs_library_id)
        if library is None or not await library_can_write(
            self.session, library, user_id
        ):
            raise PermissionError("Docs workspaceへの書き込み権限がありません")

        day_iso = day.isoformat()
        # Day タグ（resolve_supertag は Day を作成/取得できる）。
        day_tag = await self.resolve_supertag(docs_library_id=docs_library_id, tag="Day", create=True)

        iso_year, iso_week, _ = day.isocalendar()
        daily_root = await self._ensure_system_node(
            docs_library_id=docs_library_id, title="Daily notes", parent_id=None, sort_order=10, user_id=user_id
        )
        year_root = await self._ensure_system_node(
            docs_library_id=docs_library_id, title=str(day.year), parent_id=daily_root.id,
            sort_order=float(day.year), user_id=user_id,
        )
        week_root = await self._ensure_system_node(
            docs_library_id=docs_library_id, title=f"Week {iso_week:02d}", parent_id=year_root.id,
            sort_order=float(iso_week), user_id=user_id,
        )

        existing_result = await self.session.execute(
            select(KnowledgeNode)
            .where(
                KnowledgeNode.docs_library_id == docs_library_id,
                KnowledgeNode.day_date == day,
                KnowledgeNode.archived_at.is_(None),
            )
            .order_by(KnowledgeNode.created_at)
            .limit(1)
        )
        day_node = existing_result.scalar_one_or_none()
        if day_node is not None:
            await self._ensure_write_access(
                day_node,
                user_id,
                project_id=_coerce_uuid(getattr(day_node, "project_id", None)),
            )
            if day_node.docs_library_id != docs_library_id:
                raise ValueError("Daily nodeと作成先workspaceが一致しません")
            if day_node.parent_id != week_root.id or day_node.root_page_id != daily_root.id:
                day_node.parent_id = week_root.id
                day_node.root_page_id = daily_root.id
                day_node.updated_by = user_id
                day_node.updated_at = _now()
                await self.session.flush()
        else:
            title = f"{day.year}年{day.month}月{day.day}日"
            day_node = await self.create_node(
                docs_library_id=docs_library_id,
                user_id=user_id,
                title=title,
                parent=week_root,
                node_type="day",
                day_date=day,
            )
            # create_node は root_page を親(week)基準にするため Web に合わせて Daily notes へ寄せる。
            day_node.root_page_id = daily_root.id
            await self.session.flush()

        await self.add_tag(node=day_node, tag=day_tag, user_id=user_id)
        tags_result = await self.session.execute(
            select(KnowledgeNodeSupertag).where(KnowledgeNodeSupertag.node_id == day_node.id)
        )
        node_supertags = list(tags_result.scalars().all())
        return day_node, day_tag, node_supertags

    async def ensure_child_sections(
        self,
        *,
        parent: KnowledgeNode,
        titles: Iterable[str],
        user_id: uuid.UUID | None,
        body_by_title: dict[str, str] | None = None,
    ) -> list[KnowledgeNode]:
        existing_result = await self.session.execute(
            select(KnowledgeNode)
            .where(
                KnowledgeNode.docs_library_id == parent.docs_library_id,
                KnowledgeNode.parent_id == parent.id,
                KnowledgeNode.archived_at.is_(None),
            )
            .order_by(KnowledgeNode.sort_order, KnowledgeNode.created_at)
        )
        existing_by_title = {node.title: node for node in existing_result.scalars().all()}
        sections: list[KnowledgeNode] = []
        for title in titles:
            section = existing_by_title.get(title)
            if section is None:
                section = await self.create_node(
                    docs_library_id=parent.docs_library_id,
                    user_id=user_id,
                    parent=parent,
                    project_id=parent.project_id,
                    title=title,
                )
                initial_body = (body_by_title or {}).get(title, "").strip()
                if initial_body:
                    content_parent = await self._ensure_section_content_container(
                        section=section,
                        user_id=user_id,
                    )
                    await self.create_nodes_from_outline(
                        docs_library_id=parent.docs_library_id,
                        user_id=user_id,
                        parent=content_parent,
                        outline_text=initial_body,
                        project_id=parent.project_id,
                    )
            elif (section.body_text or "").strip() not in {"", _title_mirror(section.title)}:
                legacy_body = section.body_text
                content_parent = await self._ensure_section_content_container(
                    section=section,
                    user_id=user_id,
                )
                await self.create_nodes_from_outline(
                    docs_library_id=parent.docs_library_id,
                    user_id=user_id,
                    parent=content_parent,
                    outline_text=legacy_body,
                    project_id=parent.project_id,
                )
                section.body_text = _title_mirror(section.title)
            sections.append(section)
        return sections

    async def _ensure_section_content_container(
        self,
        *,
        section: KnowledgeNode,
        user_id: uuid.UUID | None,
    ) -> KnowledgeNode:
        system_key = f"docs_section_content:{section.id}"
        result = await self.session.execute(
            select(KnowledgeNode)
            .where(
                KnowledgeNode.docs_library_id == section.docs_library_id,
                KnowledgeNode.system_key == system_key,
                KnowledgeNode.archived_at.is_(None),
            )
            .limit(1)
        )
        container = result.scalar_one_or_none()
        if container is not None:
            return container
        return await self.create_node(
            docs_library_id=section.docs_library_id,
            user_id=user_id,
            parent=section,
            project_id=section.project_id,
            title="内容",
            system_key=system_key,
            body_json={"format": "doc_block", "block_type": "content_container"},
        )

    async def append_to_section(
        self,
        *,
        parent: KnowledgeNode,
        section_title: str,
        text: str,
        operation: str,
        user_id: uuid.UUID | None,
    ) -> KnowledgeNode:
        sections = await self.ensure_child_sections(
            parent=parent,
            titles=[section_title],
            user_id=user_id,
        )
        section = sections[0]
        body = str(text or "").strip()
        if not body:
            return section
        content_parent = await self._ensure_section_content_container(
            section=section,
            user_id=user_id,
        )
        if (section.body_text or "").strip() not in {"", _title_mirror(section.title)}:
            legacy_body = section.body_text
            await self.create_nodes_from_outline(
                docs_library_id=parent.docs_library_id,
                user_id=user_id,
                parent=content_parent,
                outline_text=legacy_body,
                project_id=parent.project_id,
            )
            section.body_text = _title_mirror(section.title)
        if str(operation or "append").casefold() == "replace":
            content_parent.system_key = f"docs_section_content_archived:{section.id}:{content_parent.id}"
            await self.archive_subtree(
                root=content_parent,
                user_id=user_id,
            )
            content_parent = await self._ensure_section_content_container(
                section=section,
                user_id=user_id,
            )
        await self.create_nodes_from_outline(
            docs_library_id=parent.docs_library_id,
            user_id=user_id,
            parent=content_parent,
            outline_text=body,
            project_id=parent.project_id,
        )
        section.body_text = _title_mirror(section.title)
        section.updated_by = user_id
        section.updated_at = _now()
        await self.record_node_change(section, user_id, f"{section_title}を更新")
        await self.session.flush()
        return section

    async def search(
        self,
        *,
        docs_library_id: uuid.UUID | None = None,
        workspace_id: uuid.UUID | None = None,
        query: str = "",
        project_id: uuid.UUID | None = None,
        tag: str = "",
        limit: int = 20,
        user_id: uuid.UUID | None = None,
    ) -> list[KnowledgeNode]:
        docs_library_id = _resolve_docs_library_id(docs_library_id, workspace_id)
        stmt = (
            select(KnowledgeNode)
            .join(KnowledgeSearchIndex, KnowledgeSearchIndex.node_id == KnowledgeNode.id)
            .where(KnowledgeNode.docs_library_id == docs_library_id, KnowledgeNode.archived_at.is_(None))
        )
        if user_id is not None:
            library_row = await self.session.get(DocsLibrary, docs_library_id)
            # Visibility is composed into the candidate SQL.  Do not fetch
            # the library's accessible IDs into Python before applying the
            # search LIMIT (the old path became a 150k-element IN predicate).
            stmt = apply_docs_visibility(
                stmt,
                docs_library_id=docs_library_id,
                user_id=user_id,
                node_model=KnowledgeNode,
                library_owner_id=getattr(library_row, "owner_user_id", None),
            )
        if project_id is not None:
            stmt = stmt.where(KnowledgeNode.project_id == project_id)
        id_rank = None
        if query.strip():
            query_text = query.strip()
            like_term = f"%{query_text}%"
            email_body_match = (
                select(KnowledgeFieldValue.node_id)
                .join(KnowledgeField, KnowledgeField.id == KnowledgeFieldValue.field_id)
                .where(
                    KnowledgeFieldValue.node_id == KnowledgeNode.id,
                    KnowledgeField.system_key == "email_body",
                    KnowledgeFieldValue.value_text.ilike(like_term),
                )
                .exists()
            )
            lexical_match = or_(
                KnowledgeSearchIndex.title_text.ilike(like_term),
                KnowledgeSearchIndex.body_text_plain.ilike(like_term),
                email_body_match,
            )
            # Lane 19/20 ID contract: full UUID, hyphenless UUID, and an
            # 8+ hex prefix all participate in the same ACL-filtered query.
            # ID hits sort before ordinary lexical hits but never bypass the
            # project/library visibility predicate above.
            normalized_id = query_text.replace("-", "")
            id_match = None
            if re.fullmatch(r"[0-9a-fA-F]{8,32}", normalized_id):
                id_match = func.replace(
                    cast(KnowledgeNode.id, String), "-", ""
                ).ilike(f"{normalized_id}%")
            elif _coerce_uuid(query_text) is not None:
                id_match = KnowledgeNode.id == _coerce_uuid(query_text)
            if id_match is not None:
                stmt = stmt.where(or_(id_match, lexical_match))
                id_rank = case((id_match, 0), else_=1)
            else:
                stmt = stmt.where(lexical_match)
        if tag.strip():
            tag_row = await self.resolve_supertag(docs_library_id=docs_library_id, tag=tag, create=False)
            stmt = stmt.join(
                KnowledgeNodeSupertag,
                KnowledgeNodeSupertag.node_id == KnowledgeNode.id,
            ).join(
                KnowledgeSupertag,
                KnowledgeSupertag.id == KnowledgeNodeSupertag.supertag_id,
            ).where(
                KnowledgeNodeSupertag.supertag_id == tag_row.id,
                KnowledgeSupertag.id == tag_row.id,
                KnowledgeSupertag.docs_library_id == docs_library_id,
            )
        order_columns = []
        if id_rank is not None:
            order_columns.append(id_rank)
        order_columns.append(KnowledgeNode.updated_at.desc())
        stmt = stmt.order_by(*order_columns).limit(max(1, min(int(limit or 20), 100)))
        result = await self.session.execute(stmt)
        return list(result.scalars().unique().all())

    @staticmethod
    def _merge_scoped_nodes(
        *,
        candidates: Iterable[KnowledgeNode],
        docs_scope: DocsScope,
        limit: int,
    ) -> list[KnowledgeNode]:
        """Filter and rank already ACL-authorized Docs candidates.

        ``DocsScope`` is intentionally an identifier-only boundary.  The
        underlying per-library search/query remains responsible for its ACL;
        this method only applies the resolved node lanes and never treats the
        scope's project IDs as a visibility predicate.
        """

        canonical_order = {
            normalized_id: index
            for index, raw_node_id in enumerate(docs_scope.canonical_node_ids)
            if (normalized_id := _coerce_uuid(raw_node_id)) is not None
        }
        canonical_ids = {
            normalized_id
            for raw_node_id in docs_scope.canonical_node_ids
            if (normalized_id := _coerce_uuid(raw_node_id)) is not None
        }
        related_order = {
            normalized_id: index
            for index, raw_node_id in enumerate(docs_scope.related_node_ids)
            if (normalized_id := _coerce_uuid(raw_node_id)) is not None
        }
        allowed_ids = canonical_ids | set(related_order)
        allowed_libraries = {
            normalized_id
            for raw_library_id in docs_scope.allowed_library_ids
            if (normalized_id := _coerce_uuid(raw_library_id)) is not None
        }
        if not allowed_ids or not allowed_libraries or limit <= 0:
            return []

        # Keep the first candidate order as a stable tie-break, but allow a
        # later occurrence to replace it when the same node is encountered in
        # a more relevant lane (for example, canonical and related overlap).
        selected: dict[
            uuid.UUID,
            tuple[tuple[int, int, int, int, str], int, KnowledgeNode],
        ] = {}
        for order, node in enumerate(candidates):
            node_id = _coerce_uuid(getattr(node, "id", None))
            if node_id is None or node_id not in allowed_ids:
                continue
            library_id = _coerce_uuid(getattr(node, "docs_library_id", None))
            if library_id is None or library_id not in allowed_libraries:
                continue

            if node_id in canonical_ids:
                lane = 0
                lane_order = canonical_order[node_id]
            else:
                lane_order = related_order[node_id]
                # Personal nodes are deliberately lower priority than
                # project-related references while retaining resolver order.
                is_personal = (
                    docs_scope.personal_allowed
                    and getattr(node, "project_id", None) is None
                )
                lane = 2 if is_personal else 1
            # If a duplicate ID is returned from multiple library lanes, keep
            # the project-bearing row over a personal row before falling back
            # to the underlying result order and ID for stability.
            project_preference = 1 if getattr(node, "project_id", None) is None else 0
            rank = (lane, lane_order, project_preference, order, str(node_id))
            previous = selected.get(node_id)
            if previous is None or rank < previous[0]:
                selected[node_id] = (rank, order, node)

        ranked = sorted(
            selected.values(),
            key=lambda item: item[0],
        )
        return [node for _, _, node in ranked[:limit]]

    async def search_with_scope(
        self,
        *,
        query: str,
        docs_scope: DocsScope,
        limit: int = 20,
    ) -> list[KnowledgeNode]:
        """Search each allowed library, then enforce the resolved node lanes."""

        global_limit = min(int(limit or 0), 100)
        if global_limit <= 0 or not docs_scope.allowed_library_ids:
            return []
        per_library_limit = min(global_limit, 20)
        candidates: list[KnowledgeNode] = []
        for library_id in docs_scope.allowed_library_ids:
            candidates.extend(
                await self.search(
                    docs_library_id=library_id,
                    query=query,
                    limit=per_library_limit,
                )
            )
        return self._merge_scoped_nodes(
            candidates=candidates,
            docs_scope=docs_scope,
            limit=global_limit,
        )

    async def outline_lines(
        self,
        *,
        root: KnowledgeNode,
        depth: int = 3,
        node_filter: Callable[[KnowledgeNode], Awaitable[bool]] | None = None,
        user_id: uuid.UUID | None = None,
    ) -> list[str]:
        max_depth = max(0, min(int(depth or 3), 8))
        if user_id is not None and not await can_read_node(self.session, root, user_id):
            return []
        nodes = [root]
        frontier = [root.id]
        truncated = False
        for _ in range(max_depth):
            if not frontier or len(nodes) >= 500:
                truncated = truncated or len(nodes) >= 500
                break
            remaining = 500 - len(nodes)
            result = await self.session.execute(
                select(KnowledgeNode)
                .where(
                    KnowledgeNode.docs_library_id == root.docs_library_id,
                    KnowledgeNode.parent_id.in_(frontier),
                    KnowledgeNode.archived_at.is_(None),
                    *(
                        [KnowledgeNode.project_id == root.project_id]
                        if root.project_id is not None
                        else []
                    ),
                )
                .order_by(KnowledgeNode.sort_order, KnowledgeNode.created_at)
                .limit(remaining + 1)
            )
            level = list(result.scalars().unique().all())
            if user_id is not None:
                level = [
                    node
                    for node in level
                    if await can_read_node(self.session, node, user_id)
                ]
            if len(level) > remaining:
                truncated = True
                level = level[:remaining]
            nodes.extend(level)
            frontier = [node.id for node in level]
        children: dict[uuid.UUID | None, list[KnowledgeNode]] = {}
        for node in nodes:
            children.setdefault(node.parent_id, []).append(node)

        tag_rows = await self.session.execute(
            select(KnowledgeNodeSupertag.node_id, KnowledgeSupertag.name)
            .join(KnowledgeSupertag, KnowledgeNodeSupertag.supertag_id == KnowledgeSupertag.id)
            .where(
                KnowledgeNodeSupertag.node_id.in_([node.id for node in nodes]),
                KnowledgeSupertag.docs_library_id == root.docs_library_id,
            )
        )
        tags_by_node: dict[uuid.UUID, list[str]] = {}
        for node_id, tag_name in tag_rows.all():
            tags_by_node.setdefault(node_id, []).append(tag_name)

        lines: list[str] = []

        async def visit(node: KnowledgeNode, current_depth: int) -> None:
            if current_depth > max_depth:
                return
            if node_filter is not None and not await node_filter(node):
                return
            tags = " ".join(f"#{name}" for name in tags_by_node.get(node.id, []))
            suffix = f" {tags}" if tags else ""
            indent = "\t" * current_depth
            lines.append(f"{indent}{_short_id(node.id)} {node.title}{suffix}")
            for child in children.get(node.id, []):
                await visit(child, current_depth + 1)

        await visit(root, 0)
        if truncated:
            lines.append(
                "... outline truncated at 500 nodes; Inboxの全文置換は拒否されます。"
            )
        return lines

    async def format_search_results(
        self,
        nodes: list[KnowledgeNode],
        *,
        user_id: uuid.UUID | None = None,
    ) -> str:
        if user_id is not None:
            nodes = [
                node
                for node in nodes
                if await can_read_node(self.session, node, user_id)
            ]
        if not nodes:
            return "No Docs nodes found."
        node_ids = [node.id for node in nodes]
        tag_rows = await self.session.execute(
            select(KnowledgeNodeSupertag.node_id, KnowledgeSupertag.name)
            .join(KnowledgeSupertag, KnowledgeNodeSupertag.supertag_id == KnowledgeSupertag.id)
            .where(
                KnowledgeNodeSupertag.node_id.in_(node_ids),
                KnowledgeSupertag.docs_library_id == nodes[0].docs_library_id,
            )
        )
        tags_by_node: dict[uuid.UUID, list[str]] = {}
        for node_id, tag_name in tag_rows.all():
            tags_by_node.setdefault(node_id, []).append(tag_name)
        parents_by_node = await self._parent_titles(nodes, user_id=user_id)
        lines = []
        for node in nodes:
            tags = " ".join(f"#{name}" for name in tags_by_node.get(node.id, [])[:5])
            project = f" project={_short_id(node.project_id)}" if node.project_id else ""
            tag_text = f" {tags}" if tags else ""
            parent_title = parents_by_node.get(node.id)
            parent_text = f" ⤷ {parent_title}" if parent_title else ""
            lines.append(f"{_short_id(node.id)} | {node.title}{tag_text}{project}{parent_text}")
        return "\n".join(lines)

    async def _parent_titles(
        self,
        nodes: list[KnowledgeNode],
        *,
        user_id: uuid.UUID | None = None,
    ) -> dict[uuid.UUID, str]:
        """Return immediate parent titles only when the full path is readable."""
        parent_ids = {node.parent_id for node in nodes if node.parent_id is not None}
        if not parent_ids:
            return {}
        result = await self.session.execute(
            select(
                KnowledgeNode.id,
                KnowledgeNode.title,
                KnowledgeNode.docs_library_id,
            ).where(KnowledgeNode.id.in_(parent_ids))
        )
        parent_rows = {
            row_id: (title, docs_library_id)
            for row_id, title, docs_library_id in result.all()
        }
        if user_id is None:
            return {
                node.id: parent_rows[node.parent_id][0]
                for node in nodes
                if node.parent_id in parent_rows
                and parent_rows[node.parent_id][1] == node.docs_library_id
            }

        readable: dict[uuid.UUID, str] = {}
        for node in nodes:
            parent_row = parent_rows.get(node.parent_id)
            if parent_row is None or parent_row[1] != node.docs_library_id:
                continue
            current = await self.session.get(KnowledgeNode, node.parent_id)
            seen: set[uuid.UUID] = set()
            all_readable = True
            while current is not None and current.id not in seen:
                if current.docs_library_id != node.docs_library_id:
                    all_readable = False
                    break
                seen.add(current.id)
                if not await can_read_node(self.session, current, user_id):
                    all_readable = False
                    break
                current = (
                    await self.session.get(KnowledgeNode, current.parent_id)
                    if current.parent_id is not None
                    else None
                )
            if all_readable:
                readable[node.id] = parent_row[0]
        return readable

    async def ancestor_titles(
        self,
        node: KnowledgeNode,
        max_depth: int = 8,
        *,
        user_id: uuid.UUID | None = None,
    ) -> list[str]:
        """Return ancestor titles from the root down to (but excluding) node."""
        titles: list[str] = []
        seen: set[uuid.UUID] = {node.id}
        current = node
        for _ in range(max_depth):
            parent_id = current.parent_id
            if parent_id is None or parent_id in seen:
                break
            seen.add(parent_id)
            parent = await self.session.get(KnowledgeNode, parent_id)
            if parent is None:
                break
            if parent.docs_library_id != node.docs_library_id:
                return []
            if user_id is not None and not await can_read_node(
                self.session, parent, user_id
            ):
                return []
            titles.append(parent.title or "")
            current = parent
        return list(reversed(titles))

    async def get_backlinks(
        self,
        node: KnowledgeNode,
        limit: int = 50,
        user_id: uuid.UUID | None = None,
    ) -> list[KnowledgeNode]:
        """Return nodes that reference this node via inline `[[...]]` edges."""
        stmt = (
            select(KnowledgeNode)
            .join(KnowledgeEdge, KnowledgeEdge.source_node_id == KnowledgeNode.id)
            .where(
                KnowledgeEdge.target_node_id == node.id,
                KnowledgeEdge.relation_type.in_(["inline_ref", "references"]),
                KnowledgeNode.docs_library_id == node.docs_library_id,
                KnowledgeNode.archived_at.is_(None),
            )
        )
        if user_id is not None:
            library_row = await self.session.get(DocsLibrary, node.docs_library_id)
            stmt = apply_docs_visibility(
                stmt,
                docs_library_id=node.docs_library_id,
                user_id=user_id,
                node_model=KnowledgeNode,
                library_owner_id=getattr(library_row, "owner_user_id", None),
            )
        result = await self.session.execute(
            stmt.order_by(KnowledgeNode.updated_at.desc()).limit(
                max(1, min(int(limit or 50), 200))
            )
        )
        rows = list(result.scalars().unique().all())
        return rows

    def _format_field_value(self, field: KnowledgeField, value: KnowledgeFieldValue) -> str:
        field_type = str(field.field_type or "text")
        if field_type == "number" and value.value_number is not None:
            number = value.value_number
            return str(int(number)) if float(number).is_integer() else str(number)
        if field_type == "date" and value.value_datetime is not None:
            return value.value_datetime.isoformat()
        if field_type == "checkbox" and isinstance(value.value_json, dict):
            return "true" if value.value_json.get("value") else "false"
        if field_type == "reference" and value.target_node_id is not None:
            return f"[[node:{value.target_node_id}]]"
        if value.value_text is not None:
            return value.value_text
        if value.value_json is not None:
            return str(value.value_json)
        return ""

    async def _get_bound_task(self, node: KnowledgeNode) -> Task | None:
        result = await self.session.execute(
            select(Task).where(Task.knowledge_node_id == node.id, Task.deleted_at.is_(None)).limit(1)
        )
        return result.scalar_one_or_none()

    async def _can_read_bound_task_metadata(
        self,
        *,
        node: KnowledgeNode,
        task: Task,
        user_id: uuid.UUID | None,
    ) -> bool:
        """Check ACL before exposing synthetic Task fields through Docs.

        A shared Docs node is not itself a grant to task metadata.  Project
        tasks use the Project read ACL; legacy projectless tasks are private to
        the owner of a personal library and are never exposed through an
        explicit subtree share.
        """

        if user_id is None:
            return True
        task_project_id = _coerce_uuid(getattr(task, "project_id", None))
        if task_project_id is not None:
            try:
                return await ProjectRepository.has_permission(
                    self.session,
                    project_id=task_project_id,
                    user_id=user_id,
                    permission="read",
                )
            except Exception:
                return False
        library = await self.session.get(DocsLibrary, node.docs_library_id)
        if library is None:
            return False
        return (
            str(getattr(library, "library_type", "personal") or "personal").lower()
            == "personal"
            and _coerce_uuid(getattr(library, "owner_user_id", None)) == user_id
        )

    async def get_node_field_values(
        self,
        node: KnowledgeNode,
        *,
        user_id: uuid.UUID | None = None,
    ) -> dict[str, str]:
        """Return current field name -> display value for a node.

        Includes Docs-native field values (``KnowledgeFieldValue``) and, when the
        node is bound to a task via ``#Task``, the current task-system field values
        (status/due/start/priority) which live on the task, not on the node.
        """
        result = await self.session.execute(
            select(KnowledgeField, KnowledgeFieldValue)
            .join(KnowledgeFieldValue, KnowledgeFieldValue.field_id == KnowledgeField.id)
            .where(
                KnowledgeFieldValue.node_id == node.id,
                KnowledgeField.docs_library_id == node.docs_library_id,
            )
            .order_by(KnowledgeField.sort_order, KnowledgeField.created_at)
        )
        values: dict[str, str] = {}
        for field, value in result.all():
            if (
                user_id is not None
                and str(field.field_type or "") == "reference"
                and value.target_node_id is not None
                and not await can_read_node(self.session, value.target_node_id, user_id)
            ):
                continue
            rendered = self._format_field_value(field, value)
            if rendered != "":
                values[field.name] = rendered

        task = await self._get_bound_task(node)
        if task is not None and await self._can_read_bound_task_metadata(
            node=node,
            task=task,
            user_id=user_id,
        ):
            fields_by_ref = await self.resolve_node_fields(node)
            if any(key in fields_by_ref for key in TASK_FIELD_TO_TASK_UPDATE):
                for system_key, task_attr in TASK_FIELD_TO_TASK_UPDATE.items():
                    field = fields_by_ref.get(system_key)
                    if field is None:
                        continue
                    raw = getattr(task, task_attr, None)
                    if raw in (None, ""):
                        continue
                    values[field.name] = raw.isoformat() if isinstance(raw, datetime) else str(raw)
        return values

    async def query_nodes(
        self,
        *,
        docs_library_id: uuid.UUID | None = None,
        workspace_id: uuid.UUID | None = None,
        tags: list[str] | None = None,
        text: str = "",
        project_id: uuid.UUID | None = None,
        field_filters: dict[str, str] | None = None,
        limit: int = 50,
        user_id: uuid.UUID | None = None,
    ) -> list[KnowledgeNode]:
        """Structured query: AND over tags, optional field equality, text ILIKE."""
        docs_library_id = _resolve_docs_library_id(docs_library_id, workspace_id)
        stmt = select(KnowledgeNode).where(
            KnowledgeNode.docs_library_id == docs_library_id,
            KnowledgeNode.archived_at.is_(None),
        )
        shared_nodes = None
        library_row = None
        if user_id is not None:
            library_row = await self.session.get(DocsLibrary, docs_library_id)
            actor = _coerce_uuid(user_id)
            if actor is not None and getattr(library_row, "owner_user_id", None) != actor:
                shared_nodes = _shared_nodes_cte(
                    docs_library_id=docs_library_id,
                    user_id=actor,
                    name="docs_query_shared_nodes",
                )
            stmt = apply_docs_visibility(
                stmt,
                docs_library_id=docs_library_id,
                user_id=user_id,
                node_model=KnowledgeNode,
                library_owner_id=getattr(library_row, "owner_user_id", None),
                shared_nodes=shared_nodes,
            )
        if project_id is not None:
            stmt = stmt.where(KnowledgeNode.project_id == project_id)
        if text.strip():
            stmt = stmt.where(KnowledgeNode.title.ilike(f"%{text.strip()}%"))
        for tag_name in tags or []:
            tag_name = str(tag_name).strip().lstrip("#")
            if not tag_name:
                continue
            tag_row = await self.resolve_supertag(
                docs_library_id=docs_library_id, tag=tag_name, create=False
            )
            tag_exists = (
                select(KnowledgeNodeSupertag.node_id)
                .where(
                    KnowledgeNodeSupertag.node_id == KnowledgeNode.id,
                    KnowledgeNodeSupertag.supertag_id == tag_row.id,
                    KnowledgeSupertag.docs_library_id == docs_library_id,
                )
                .exists()
            )
            stmt = stmt.where(tag_exists)
        for field_name, expected in (field_filters or {}).items():
            field_name = str(field_name).strip()
            if not field_name:
                continue
            expected_text = str(expected).strip()
            field_match = (
                select(KnowledgeFieldValue.node_id)
                .join(KnowledgeField, KnowledgeField.id == KnowledgeFieldValue.field_id)
                .where(
                    KnowledgeFieldValue.node_id == KnowledgeNode.id,
                    KnowledgeField.docs_library_id == docs_library_id,
                    or_(
                        func.lower(KnowledgeField.name) == field_name.casefold(),
                        func.lower(KnowledgeField.system_key) == field_name.casefold(),
                    ),
                    or_(
                        func.lower(func.coalesce(KnowledgeFieldValue.value_text, "")) == expected_text.casefold(),
                        cast(KnowledgeFieldValue.value_number, String) == expected_text,
                        cast(KnowledgeFieldValue.target_node_id, String).ilike(f"{expected_text}%"),
                    ),
                )
                .exists()
            )
            if user_id is not None:
                target_node = aliased(KnowledgeNode)
                target_visible = (
                    select(target_node.id)
                    .where(
                        target_node.id == KnowledgeFieldValue.target_node_id,
                        docs_readable_node_predicate(
                            target_node,
                            docs_library_id=docs_library_id,
                            user_id=user_id,
                            library_owner_id=getattr(library_row, "owner_user_id", None),
                            shared_nodes=shared_nodes,
                        ),
                    )
                    .exists()
                )
                field_match = field_match.where(
                    or_(
                        KnowledgeFieldValue.target_node_id.is_(None),
                        target_visible,
                    )
                )
            stmt = stmt.where(field_match)
        stmt = stmt.order_by(KnowledgeNode.updated_at.desc()).limit(
            max(1, min(int(limit or 50), 200))
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().unique().all())

    async def query_with_scope(
        self,
        *,
        docs_scope: DocsScope,
        tags: list[str] | None = None,
        text: str | None = None,
        limit: int = 20,
    ) -> list[KnowledgeNode]:
        """Run structured queries across the resolved Docs scope."""

        global_limit = min(int(limit or 0), 100)
        if global_limit <= 0 or not docs_scope.allowed_library_ids:
            return []
        per_library_limit = min(global_limit, 20)
        candidates: list[KnowledgeNode] = []
        for library_id in docs_scope.allowed_library_ids:
            candidates.extend(
                await self.query_nodes(
                    docs_library_id=library_id,
                    tags=tags,
                    text=text or "",
                    limit=per_library_limit,
                )
            )
        return self._merge_scoped_nodes(
            candidates=candidates,
            docs_scope=docs_scope,
            limit=global_limit,
        )


# The generic ``workspace`` alias is retained only for the legacy sync/tool
# boundary.  Project-library method names are intentionally not exposed: a
# Project no longer owns a separate Docs Library and current callers resolve
# the owner's Personal library through the ``*_project_information_library``
# methods above.
DocsGraphService.ensure_workspace = DocsGraphService.ensure_library  # type: ignore[attr-defined]
