"""Shared service layer for AoiTalk Docs graph operations."""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from sqlalchemy import String, and_, case, cast, delete, func, literal, or_, select, text, union_all
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy import inspect as sa_inspect

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
    KnowledgeSupertagField,
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
    docs_node_renderable_predicate,
    library_can_write,
)
from .clip_ingest_policy import is_film_docs_node
from .docs_scope import DocsScope
from .docs_consistency import docs_id_predicate
from .task_management_service import TaskManagementError, TaskManagementService


SYSTEM_TASK_TAG = "task"
TASK_FIELD_TO_TASK_UPDATE = {
    "task_status": "status",
    "task_due": "end_at",
    "task_start": "start_at",
    "task_priority": "priority",
    "task_project": "project_id",
}

# Python str.strip() whitespace, also used by the defensive email-tree check.
_QUERY_STRIP_CHARS = (
    "\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)

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


@dataclass(frozen=True)
class DocsQueryResult:
    """A bounded Docs query page with exact match metadata.

    ``nodes`` remains deliberately bounded by the caller's requested limit.
    The count and grouping metadata are computed from the same ACL-filtered
    candidate relation before offset/limit is applied. ``truncated`` means the
    page omits any matches; ``has_more`` means matches follow this page.
    """

    nodes: list[KnowledgeNode]
    total_matches: int
    returned: int
    truncated: bool
    has_more: bool
    group_counts: dict[str, int]
    offset: int = 0

    @property
    def count(self) -> int:
        """Compatibility alias for the historical returned-row count."""

        return self.returned

    @property
    def next_offset(self) -> int | None:
        return self.offset + self.returned if self.has_more and self.returned else None


def _now() -> datetime:
    return datetime.utcnow()


def _title_mirror(title: Any) -> str:
    """title 由来の検索ミラー本文を返す（不変条件: 改行禁止・20,000字以内、空白保持）。

    Web `docsNodeTitleMirror`（docs-node-writer.ts）と同一挙動。body_text は本文正本
    ではなく title のミラーであり、Web↔モバイル往復で検索インデックス・暗号化を
    一致させるためここで一元生成する。
    """
    mirror = str(title or "")
    if "\n" in mirror or "\r" in mirror:
        raise ValueError("Docs node body_text mirror must not contain newlines")
    if len(mirror) > 20_000:
        raise ValueError("Docs node title must be 20,000 characters or less")
    return mirror


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

    async def _project_pointer_for_node(self, node_id: uuid.UUID) -> Project | None:
        """Return any persisted Project reverse pointer, including stale rows."""

        if not isinstance(self.session, AsyncSession):
            return None
        result = await self.session.execute(
            select(Project)
            .where(Project.knowledge_node_id == node_id)
            .limit(2)
        )
        rows = result.scalars().all()
        if len(rows) > 1:
            raise ValueError("複数のProjectが同じDocs nodeを参照しているため操作を中止しました")
        return rows[0] if rows else None

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
        project = await self._project_pointer_for_node(node_id)
        if project is None:
            return None
        node = await self.session.get(KnowledgeNode, node_id)
        if (
            node is None
            or node.project_id != project.id
            or str(getattr(node, "system_key", "") or "").strip()
            != f"project_information:{project.id}"
            or getattr(node, "node_type", "node") != "node"
            or getattr(node, "is_explicit_blank", False) is True
        ):
            return None
        library = await self.session.get(DocsLibrary, node.docs_library_id)
        if (
            library is None
            or str(getattr(library, "library_type", "personal") or "personal").lower() != "personal"
            or library.owner_user_id != project.owner_id
            or node.archived_at is not None
            or node.parent_id is None
            or node.root_page_id != node.parent_id
        ):
            return None
        parent = await self.session.get(KnowledgeNode, node.parent_id)
        if (
            parent is None
            or parent.docs_library_id != library.id
            or parent.system_key != "project_information_root"
            or parent.title != "案件情報"
            or parent.parent_id is not None
            or parent.root_page_id not in (None, parent.id)
            or parent.archived_at is not None
        ):
            return None
        tag_result = await self.session.execute(
            select(KnowledgeNodeSupertag.node_id)
            .join(KnowledgeSupertag, KnowledgeSupertag.id == KnowledgeNodeSupertag.supertag_id)
            .where(
                KnowledgeNodeSupertag.node_id == node.id,
                KnowledgeSupertag.docs_library_id == library.id,
                KnowledgeSupertag.system_key == "project_info",
            )
            .limit(1)
        )
        if tag_result.scalar_one_or_none() is None:
            return None
        return project

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
            select(KnowledgeNode.title, KnowledgeNode.archived_at)
            .where(
                KnowledgeNode.id == parent.id,
                KnowledgeNode.docs_library_id == docs_library_id,
            )
            .with_for_update()
        )
        parent_row = locked_parent.first()
        if parent_row is not None and parent_row[1] is not None:
            raise ValueError("アーカイブ済みnodeの下には作成/移動できません")
        parent_title = parent_row[0] if parent_row is not None else None
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
        if parent is not None and getattr(parent, "archived_at", None) is not None:
            raise ValueError("アーカイブ済みnodeの下には作成できません")
        # Descendants inherit the canonical Project scope from their parent.
        # Resolve it before ACL enforcement so a writer does not need to send
        # a redundant project_id for every child create.  An explicit mismatch
        # is rejected rather than allowing a cross-Project bridge through a
        # direct service caller that bypassed the REST preflight.
        if parent is not None:
            parent_project_id = _coerce_uuid(getattr(parent, "project_id", None))
            if (
                parent_project_id is not None
                and project_id is not None
                and _coerce_uuid(project_id) != parent_project_id
            ):
                raise ValueError("親nodeと作成対象Projectが一致しません")
            if parent_project_id is not None:
                project_id = parent_project_id
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
        clean_title = _title_mirror(title)
        normalized_body_json = (
            dict(body_json) if isinstance(body_json, dict) else {}
        )
        explicit_blank = is_explicit_blank_paragraph(
            clean_title,
            normalized_body_json,
            node_type,
        ) and not str(system_key or "").strip()
        if not clean_title.strip() and not explicit_blank:
            raise ValueError("空行はDocs nodeとして保存できません")
        if clean_title and normalized_body_json.get("blank") is True:
            # ``blank`` is a discriminator, not arbitrary user metadata.  A
            # meaningful title must never be persisted with a stale marker.
            normalized_body_json = clear_blank_paragraph_marker(normalized_body_json)
        if (
            parent is not None
            and clean_title
            # A Project-information root may intentionally have the same
            # label as the Personal 案件情報 hub.  Ordinary children retain
            # the parent-title uniqueness invariant.
            and not str(system_key or "").strip().startswith("project_information:")
        ):
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
        elif str(body_text or "") not in {"", body_text_value}:
            raise ValueError("Docs body content must be represented by child nodes")
        node = KnowledgeNode(
            id=node_id if node_id is not None else uuid.uuid4(),
            docs_library_id=docs_library_id,
            parent_id=parent.id if parent else None,
            root_page_id=root_page_id,
            project_id=project_id,
            system_key=system_key,
            title=clean_title,
            is_explicit_blank=explicit_blank and not bool(system_key),
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
            existing.is_explicit_blank = False
            changed = True
        expected_body_json = body_json or {}
        if existing.body_json != expected_body_json:
            existing.body_json = expected_body_json
            existing.is_explicit_blank = False
            changed = True
        if existing.archived_at is not None:
            existing.archived_at = None
            changed = True
        if getattr(existing, "is_explicit_blank", False):
            # System nodes are identity-bearing and must never be projected as
            # user-created blank paragraphs.
            existing.is_explicit_blank = False
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
        # Serialize direct/tool writes with Project pointer repair.  Read the
        # current structural scope first, lock any relevant Project rows in a
        # deterministic order, then lock the target node before evaluating
        # canonical identity or mutating content.
        if isinstance(self.session, AsyncSession):
            node_identity = sa_inspect(node).identity
            node_id = node_identity[0] if node_identity else node.id
            current_result = await self.session.execute(
                select(KnowledgeNode).where(KnowledgeNode.id == node_id).limit(1)
            )
            current_row = current_result.scalar_one_or_none()
            if current_row is None:
                raise ValueError("Docs node not found")
            project_conditions: list[Any] = [Project.knowledge_node_id == node_id]
            if current_row.project_id is not None:
                project_conditions.append(Project.id == current_row.project_id)
            await self.session.execute(
                select(Project)
                .where(or_(*project_conditions))
                .order_by(Project.id)
                .with_for_update()
            )
            locked_result = await self.session.execute(
                select(KnowledgeNode).where(KnowledgeNode.id == node_id).with_for_update()
            )
            locked_node = locked_result.scalar_one_or_none()
            if locked_node is None:
                raise ValueError("Docs node not found")
            node = locked_node
        await self._ensure_write_access(
            node,
            user_id,
            project_id=_coerce_uuid(getattr(node, "project_id", None)),
        )
        pointer_project = await self._project_pointer_for_node(node.id)
        canonical_project = await self._canonical_project_for_node(node.id)
        if pointer_project is not None and canonical_project is None:
            raise ValueError("Project canonical identityを確認できないためDocs操作を中止しました")
        if canonical_project is not None:
            if canonical_project.deleted_at is not None or bool(canonical_project.is_completed):
                raise ValueError(
                    "完了/削除済みProjectのcanonical情報rootは通常のDocs操作では変更できません"
                )
            # Project metadata owns the identity label.  Generic callers may
            # retry a stale rename/blank clear, but the canonical root is
            # normalized back to the current Project name and never receives
            # an ordinary paragraph blank envelope.
            title = str(canonical_project.name or "").strip() or "案件情報"
            if isinstance(body_json, dict) and is_explicit_blank_paragraph(
                "", body_json, getattr(node, "node_type", "node")
            ):
                body_json = None
        identity_key = str(getattr(node, "system_key", "") or "").strip()
        if identity_key == "project_information_root" or identity_key.startswith(
            "project_information:"
        ):
            if pointer_project is None:
                raise ValueError(
                    "stale案件情報の正本nodeは専用クリーンアップ/修復経路でのみ変更できます"
                )
            title = str(title or "").strip() or str(node.title or "").strip() or "案件情報"
            if isinstance(body_json, dict) and is_explicit_blank_paragraph(
                "", body_json, getattr(node, "node_type", "node")
            ):
                body_json = None
        # ``body_json`` is the same-request discriminator for a persisted
        # blank paragraph.  Existing metadata is copied before we mutate it
        # so a nonblank transition removes only ``blank`` and never loses
        # provenance/display fields.
        supplied_body_json = isinstance(body_json, dict)
        current_body_json = (
            dict(node.body_json) if isinstance(node.body_json, dict) else {}
        )
        next_title = node.title
        # Keep the discriminator synchronized even for metadata-only updates.
        # The ORM body_json property is decrypted at this boundary, so the
        # strict body marker remains the semantic source of truth.
        explicit_blank = (
            not bool(getattr(node, "system_key", None))
            and is_explicit_blank_paragraph(
                next_title,
                current_body_json,
                getattr(node, "node_type", "node"),
            )
        )
        if title is not None:
            next_title = _title_mirror(title)
            explicit_blank = (
                is_explicit_blank_paragraph(
                    next_title,
                    body_json,
                    getattr(node, "node_type", "node"),
                )
                and not str(getattr(node, "system_key", "") or "").strip()
            )
            if not next_title.strip() and not explicit_blank:
                raise ValueError("空行はDocs nodeとして保存できません")
        elif not next_title and supplied_body_json:
            # A metadata-only update of an already blank paragraph must carry
            # the marker in that same request as well; otherwise it would
            # silently turn the row into an invalid legacy blank.
            explicit_blank = (
                is_explicit_blank_paragraph(
                    next_title,
                    body_json,
                    getattr(node, "node_type", "node"),
                )
                and not str(getattr(node, "system_key", "") or "").strip()
            )
            if not explicit_blank:
                raise ValueError("空paragraphの更新にはblank markerが必要です")

        if title is not None and next_title:
            if node.parent_id is not None:
                parent = await self.session.get(KnowledgeNode, node.parent_id)
                if parent is None:
                    raise ValueError("親nodeが見つかりません")
                if not str(getattr(node, "system_key", "") or "").strip().startswith(
                    "project_information:"
                ):
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
        node.is_explicit_blank = bool(explicit_blank and not getattr(node, "system_key", None))
        if body_text is not None:
            if not node.title:
                if body_text not in (None, ""):
                    raise ValueError("空paragraphのbody_textは空である必要があります")
                node.body_text = ""
            else:
                requested = str(body_text)
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
        task_project_id: uuid.UUID | None = None,
    ) -> bool:
        system_key = str(getattr(node, "system_key", "") or "").strip()
        if system_key == "project_information_root" or system_key.startswith(
            "project_information:"
        ):
            raise ValueError("案件情報の正本nodeは通常のDocs supertag操作で変更できません")
        node_identity = sa_inspect(node).identity
        node_id = node_identity[0] if node_identity else node.id
        if await self._project_pointer_for_node(node_id) is not None or await self._canonical_project_for_node(node_id) is not None:
            raise ValueError("Project canonical/stale nodeは通常のDocs supertag操作で変更できません")
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
            await self._ensure_bound_task(
                node=node, user_id=user_id, project_id=task_project_id,
            )
        return True

    async def remove_tag(
        self,
        *,
        node: KnowledgeNode,
        tag: KnowledgeSupertag,
        user_id: uuid.UUID | None,
    ) -> bool:
        system_key = str(getattr(node, "system_key", "") or "").strip()
        if system_key == "project_information_root" or system_key.startswith(
            "project_information:"
        ):
            raise ValueError("案件情報の正本nodeは通常のDocs supertag操作で変更できません")
        node_identity = sa_inspect(node).identity
        node_id = node_identity[0] if node_identity else node.id
        if await self._project_pointer_for_node(node_id) is not None or await self._canonical_project_for_node(node_id) is not None:
            raise ValueError("Project canonical/stale nodeは通常のDocs supertag操作で変更できません")
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

    async def _ensure_bound_task(
        self,
        *,
        node: KnowledgeNode,
        user_id: uuid.UUID | None,
        project_id: uuid.UUID | None = None,
    ) -> None:
        if user_id is None:
            return
        existing = await self.session.execute(
            select(Task.id).where(Task.knowledge_node_id == node.id, Task.deleted_at.is_(None)).limit(1)
        )
        if existing.scalar_one_or_none() is not None:
            return
        # Default preserves historical direct-graph behavior: pass the node's
        # project_id, including None, so create_task may ensure Inbox setup.
        # An explicit override is the Agent prelock path only.
        target_project_id = (
            _coerce_uuid(project_id)
            if project_id is not None
            else _coerce_uuid(getattr(node, "project_id", None))
        )
        await TaskManagementService().create_task(
            self.session,
            user_id=user_id,
            project_id=target_project_id,
            knowledge_node_id=node.id,
            title=node.title or "Untitled",
            description=node.description or None,
            source="docs",
            status="todo",
            priority="medium",
            task_metadata={"source": "docs", "knowledge_node_id": str(node.id)},
            commit=False,
        )

    async def resolve_existing_inbox_project_for_task_bind(
        self, user_id: uuid.UUID
    ) -> Project:
        """Return the actor's canonical Inbox Project without creating it."""
        project = await ProjectRepository.get_user_inbox_project(self.session, user_id)
        if project is None:
            raise TaskManagementError(
                "Inbox project is not available for this user",
                status_code=503,
            )
        return project

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

    async def resolve_schema_for_tag_ids(
        self,
        *,
        docs_library_id: uuid.UUID,
        tag_ids: Iterable[uuid.UUID],
    ) -> tuple[set[uuid.UUID], dict[str, KnowledgeField]]:
        """Resolve effective tags and Field refs from a direct-tag ID set.

        Direct tags expand through same-library parent Supertags. Fields
        include those owned by the effective tags and those shared onto them
        via KnowledgeSupertagField. The Field map uses UUID, casefolded name,
        and casefolded system_key keys.
        """
        seed_ids = sorted({uuid.UUID(str(tag_id)) for tag_id in tag_ids}, key=str)
        if not seed_ids:
            return set(), {}
        seed_result = await self.session.execute(
            select(KnowledgeSupertag.id).where(
                KnowledgeSupertag.id.in_(seed_ids),
                KnowledgeSupertag.docs_library_id == docs_library_id,
            )
        )
        library_seed = list(seed_result.scalars().all())
        if not library_seed:
            return set(), {}
        effective = select(KnowledgeSupertag.id, KnowledgeSupertag.parent_supertag_id).where(
            KnowledgeSupertag.id.in_(library_seed),
            KnowledgeSupertag.docs_library_id == docs_library_id,
        ).cte("docs_effective_field_tags", recursive=True)
        effective = effective.union(
            select(KnowledgeSupertag.id, KnowledgeSupertag.parent_supertag_id).join(
                effective, KnowledgeSupertag.id == effective.c.parent_supertag_id,
            ).where(KnowledgeSupertag.docs_library_id == docs_library_id)
        )
        effective_tag_ids = set(
            (await self.session.execute(select(effective.c.id))).scalars().all()
        )
        if not effective_tag_ids:
            return set(), {}
        field_result = await self.session.execute(
            select(KnowledgeField)
            .where(
                or_(
                    KnowledgeField.supertag_id.in_(sorted(effective_tag_ids, key=str)),
                    select(KnowledgeSupertagField.field_id).where(
                        KnowledgeSupertagField.field_id == KnowledgeField.id,
                        KnowledgeSupertagField.supertag_id.in_(
                            sorted(effective_tag_ids, key=str)
                        ),
                    ).exists(),
                ),
                KnowledgeField.docs_library_id == docs_library_id,
                select(KnowledgeSupertag.id).where(
                    KnowledgeSupertag.id == KnowledgeField.supertag_id,
                    KnowledgeSupertag.docs_library_id == docs_library_id,
                ).exists(),
            )
            .order_by(KnowledgeField.sort_order, KnowledgeField.created_at, KnowledgeField.id)
        )
        fields: dict[str, KnowledgeField] = {}
        for field in field_result.scalars().all():
            fields[str(field.id)] = field
            fields[field.name.casefold()] = field
            if field.system_key:
                fields[field.system_key.casefold()] = field
        return effective_tag_ids, fields

    async def resolve_node_fields(self, node: KnowledgeNode) -> dict[str, KnowledgeField]:
        tag_result = await self.session.execute(
            select(KnowledgeSupertag.id)
            .join(KnowledgeNodeSupertag, KnowledgeNodeSupertag.supertag_id == KnowledgeSupertag.id)
            .where(
                KnowledgeNodeSupertag.node_id == node.id,
                KnowledgeSupertag.docs_library_id == node.docs_library_id,
            )
        )
        _effective, fields = await self.resolve_schema_for_tag_ids(
            docs_library_id=node.docs_library_id,
            tag_ids=list(tag_result.scalars().all()),
        )
        return fields

    async def set_fields(
        self,
        *,
        node: KnowledgeNode,
        values: dict[str, Any],
        user_id: uuid.UUID | None,
    ) -> dict[str, str]:
        node_identity = sa_inspect(node).identity
        node_id = node_identity[0] if node_identity else node.id
        system_key = str(getattr(node, "system_key", "") or "").strip()
        if (
            system_key == "project_information_root"
            or system_key.startswith("project_information:")
            or await self._project_pointer_for_node(node_id) is not None
            or await self._canonical_project_for_node(node_id) is not None
        ):
            raise ValueError("Project canonical/stale nodeは通常のDocs field操作で変更できません")
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
        node_identity = sa_inspect(node).identity
        node_id = node_identity[0] if node_identity else node.id
        if await self._project_pointer_for_node(node_id) is not None:
            raise ValueError("Projectが参照するDocs nodeを含むため通常のDocs moveでは移動できません")
        canonical_project = await self._canonical_project_for_node(node_id)
        identity_system_key = str(getattr(node, "system_key", "") or "").strip()
        if identity_system_key == "project_information_root" or identity_system_key.startswith(
            "project_information:"
        ):
            raise ValueError("案件情報hubは通常のDocs moveでは移動できません")
        # Moving an ancestor rewrites root_page_id for its complete subtree.
        # Protect identity-bearing descendants even when a legacy row lost its
        # reverse Project pointer; otherwise a stale canonical child can be
        # buried under an ordinary move and become impossible to clean up.
        closure_result = await self.session.execute(
            text(
                """
                with recursive descendants as (
                    select id, system_key, array[id]::uuid[] as visited_path, 0 as depth
                    from knowledge_nodes
                    where id = :node_id and docs_library_id = :library_id
                    union all
                    select child.id, child.system_key,
                           parent.visited_path || array[child.id]::uuid[],
                           parent.depth + 1
                    from knowledge_nodes child
                    join descendants parent on child.parent_id = parent.id
                    where child.docs_library_id = :library_id
                      and parent.depth < 512
                      and not child.id = any(parent.visited_path)
                )
                select d.id, d.system_key, p.id as pointer_id
                from descendants d
                left join projects p on p.knowledge_node_id = d.id
                """
            ),
            {"node_id": node.id, "library_id": node.docs_library_id},
        )
        closure_rows = closure_result.all()
        if any(
            row.id != node.id
            and (
                str(row.system_key or "").strip() == "project_information_root"
                or str(row.system_key or "").strip().startswith("project_information:")
            )
            for row in closure_rows
        ):
            raise ValueError("Project canonical/stale identityを含むDocs subtreeは通常のmoveで変更できません")
        if any(row.pointer_id is not None for row in closure_rows):
            raise ValueError("Projectが参照するDocs nodeを含むため通常のDocs moveでは移動できません")
        # Recheck the closure under row locks immediately before mutation. The
        # Project rows are locked first (matching canonical repair), then the
        # source/descendant nodes; a late pointer assignment cannot slip
        # between the guard and root-page propagation.
        if isinstance(self.session, AsyncSession) and closure_rows:
            closure_ids = [row.id for row in closure_rows]
            await self.session.execute(
                select(Project.id)
                .where(Project.knowledge_node_id.in_(closure_ids))
                .order_by(Project.id)
                .with_for_update()
            )
            await self.session.execute(
                select(KnowledgeNode.id, KnowledgeNode.system_key)
                .where(
                    KnowledgeNode.docs_library_id == node.docs_library_id,
                    KnowledgeNode.id.in_(closure_ids),
                )
                .with_for_update()
            )
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
        if getattr(new_parent, "archived_at", None) is not None:
            raise ValueError("アーカイブ済みnodeの下には移動できません")
        if str(getattr(new_parent, "system_key", "") or "").strip() == "project_information_root":
            raise ValueError("案件情報hub直下への通常のDocs moveはできません")
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
        identity_system_key = str(getattr(node, "system_key", "") or "").strip()
        if (
            identity_system_key == "project_information_root"
            or identity_system_key.startswith("project_information:")
            or await self._project_pointer_for_node(node.id) is not None
            or await self._canonical_project_for_node(node.id) is not None
        ):
            raise ValueError("案件情報の正本/stale nodeはアーカイブできません")
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
        # Run the pointer/canonical authority checks before reading other ORM
        # attributes.  A caller may have rolled back a prior operation, which
        # expires those attributes; the fail-closed lookup must still surface
        # its real outage instead of triggering an implicit async refresh.
        root_identity = sa_inspect(root).identity
        root_id = root_identity[0] if root_identity else root.id
        if (
            await self._project_pointer_for_node(root_id) is not None
            or await self._canonical_project_for_node(root_id) is not None
        ):
            raise ValueError("案件情報の正本/stale nodeはアーカイブできません")
        identity_system_key = str(getattr(root, "system_key", "") or "").strip()
        if identity_system_key == "project_information_root" or identity_system_key.startswith("project_information:"):
            raise ValueError("案件情報の正本/stale nodeはアーカイブできません")
        await self._ensure_write_access(
            root,
            user_id,
            project_id=_coerce_uuid(getattr(root, "project_id", None)),
        )
        library_id = root.docs_library_id
        root_id = root_id

        async def snapshot_closure() -> list[tuple[uuid.UUID, int]]:
            result = await self.session.execute(
                text(
                    """
                    with recursive descendants as (
                        select id, parent_id, array[id]::uuid[] as visited_path, 0 as depth
                        from knowledge_nodes
                        where id = :node_id and docs_library_id = :library_id
                        union all
                        select child.id, child.parent_id,
                               parent.visited_path || array[child.id]::uuid[],
                               parent.depth + 1
                        from knowledge_nodes child
                        join descendants parent on child.parent_id = parent.id
                        where child.docs_library_id = :library_id
                          and parent.depth < 512
                          and not child.id = any(parent.visited_path)
                    )
                    select id, depth from descendants order by depth asc, id asc
                    """
                ),
                {"node_id": root_id, "library_id": library_id},
            )
            return [(row.id, int(row.depth)) for row in result]

        closure_snapshot = await snapshot_closure()
        closure_ids = [item_id for item_id, _depth in closure_snapshot]
        if not closure_ids:
            raise ValueError("Docs subtreeが見つかりません")
        pointer_result = await self.session.execute(
            select(Project.id)
            .where(Project.knowledge_node_id.in_(closure_ids))
            .order_by(Project.id)
            .with_for_update()
        )
        if pointer_result.first() is not None:
            raise ValueError("Projectが参照するDocs nodeを含むためアーカイブできません")
        locked_result = await self.session.execute(
            select(KnowledgeNode)
            .where(
                KnowledgeNode.docs_library_id == library_id,
                KnowledgeNode.id.in_(closure_ids),
            )
            .with_for_update()
        )
        locked_nodes = list(locked_result.scalars().all())
        if len(locked_nodes) != len(closure_ids):
            raise ValueError("Docs subtreeが同時変更されたためアーカイブを中止しました")
        fresh_snapshot = await snapshot_closure()
        if {item_id for item_id, _depth in fresh_snapshot} != set(closure_ids):
            raise ValueError("Docs subtreeが同時変更されたためアーカイブを中止しました")
        depth_by_id = {item_id: depth for item_id, depth in closure_snapshot}
        ordered = sorted(locked_nodes, key=lambda item: (depth_by_id.get(item.id, 0), item.id))
        if any(
            item.id != root_id
            and (
                str(getattr(item, "system_key", "") or "").strip() == "project_information_root"
                or str(getattr(item, "system_key", "") or "").strip().startswith("project_information:")
            )
            for item in ordered
        ):
            raise ValueError("Project canonical/stale identityを含むDocs subtreeはアーカイブできません")
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
            if getattr(existing, "is_explicit_blank", False):
                existing.is_explicit_blank = False
            return existing
        # Web は rootPageId=parentId（直近の親）を採用するため、それを踏襲する。
        node = KnowledgeNode(
            id=uuid.uuid4(),
            docs_library_id=docs_library_id,
            parent_id=parent_id,
            root_page_id=parent_id,
            is_explicit_blank=False,
            title=_title_mirror(title),
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
        node_ids: Iterable[uuid.UUID] | None = None,
        turn_project_id: uuid.UUID | None = None,
    ) -> list[KnowledgeNode]:
        docs_library_id = _resolve_docs_library_id(docs_library_id, workspace_id)
        stmt = (
            select(KnowledgeNode)
            .outerjoin(KnowledgeSearchIndex, KnowledgeSearchIndex.node_id == KnowledgeNode.id)
            .where(
                KnowledgeNode.docs_library_id == docs_library_id,
                KnowledgeNode.archived_at.is_(None),
                docs_node_renderable_predicate(KnowledgeNode),
            )
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
        if node_ids is not None:
            stmt = stmt.where(docs_id_predicate(KnowledgeNode.id, node_ids, self.session))
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
                KnowledgeNode.title.ilike(like_term),
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
        if query.strip():
            order_columns.append(case((func.lower(KnowledgeNode.title) == query.strip().casefold(), 0), else_=1))
        stmt = self._query_email_turn_visibility(
            stmt, docs_library_id=docs_library_id, turn_project_id=turn_project_id,
        )
        order_columns.extend([KnowledgeNode.updated_at.desc(), KnowledgeNode.id])
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
        user_id: uuid.UUID | None = None,
        tag: str = "",
        turn_project_id: uuid.UUID | None = None,
    ) -> list[KnowledgeNode]:
        """Constrain candidates by scope and current ACL before each search limit."""

        global_limit = min(int(limit or 0), 100)
        if global_limit <= 0 or not docs_scope.allowed_library_ids:
            return []
        allowed_ids = set(docs_scope.canonical_node_ids) | set(docs_scope.related_node_ids)
        if not allowed_ids:
            return []
        candidates: list[KnowledgeNode] = []
        for library_id in docs_scope.allowed_library_ids:
            try:
                matches = await self.search(
                    docs_library_id=library_id,
                    query=query,
                    limit=global_limit,
                    user_id=user_id,
                    tag=tag,
                    node_ids=allowed_ids,
                    turn_project_id=turn_project_id,
                )
            except ValueError as exc:
                if tag and str(exc).startswith("supertag not found:"):
                    continue
                raise
            candidates.extend(matches)
        ranked = self._merge_scoped_nodes(
            candidates=candidates,
            docs_scope=docs_scope,
            limit=len(candidates),
        )
        text = query.strip().casefold()
        prefix = text.replace("-", "")
        identity = bool(re.fullmatch(r"[0-9a-f]{8,32}", prefix))
        ranked.sort(key=lambda node: not (
            (identity and str(node.id).replace("-", "").startswith(prefix))
            or (text and node.title.casefold() == text)
        ))
        return ranked[:global_limit]

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
        include_parent_titles: bool = True,
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
            .join(KnowledgeNode, KnowledgeNode.id == KnowledgeNodeSupertag.node_id)
            .where(
                KnowledgeNodeSupertag.node_id.in_(node_ids),
                KnowledgeSupertag.docs_library_id == KnowledgeNode.docs_library_id,
            )
        )
        tags_by_node: dict[uuid.UUID, list[str]] = {}
        for node_id, tag_name in tag_rows.all():
            tags_by_node.setdefault(node_id, []).append(tag_name)
        parents_by_node = (
            await self._parent_titles(nodes, user_id=user_id)
            if include_parent_titles else {}
        )
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
        turn_project_id: uuid.UUID | None = None,
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
                str(field.field_type or "") == "reference"
                and value.target_node_id is not None
                and not await self._query_reference_visible(
                    value.target_node_id, user_id=user_id, turn_project_id=turn_project_id
                )
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

    async def _query_reference_visible(
        self, target_id: uuid.UUID, *, user_id: uuid.UUID | None,
        turn_project_id: uuid.UUID | None,
    ) -> bool:
        """References need both target ACL and the turn-local email boundary."""
        if user_id is not None and not await can_read_node(self.session, target_id, user_id):
            return False
        turn_id = _coerce_uuid(turn_project_id)
        if turn_id is None:
            return True
        target = await self.session.get(KnowledgeNode, target_id)
        if target is None:
            return False
        if target.project_id is None or target.project_id == turn_id:
            return True
        stmt = self._query_email_turn_visibility(
            select(KnowledgeNode.id).where(KnowledgeNode.id == target_id),
            docs_library_id=target.docs_library_id, turn_project_id=turn_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None

    @staticmethod
    def _query_email_turn_visibility(
        stmt: Any,
        *,
        docs_library_id: uuid.UUID,
        turn_project_id: uuid.UUID | None,
    ) -> Any:
        """Exclude foreign-project email trees in the query relation.

        The direct tool historically applied this rule after its bounded query
        page was loaded.  Keeping the same rule in SQL makes exact counts and
        group totals observe the same turn-local email boundary as returned
        rows. The relation starts in one Library, records the first foreign
        parent for tag parity, and uses UNION so malformed cycles terminate.
        """

        normalized_project_id = _coerce_uuid(turn_project_id)
        if normalized_project_id is None:
            return stmt

        ancestors = (
            select(
                KnowledgeNode.id.label("descendant_id"),
                KnowledgeNode.id.label("ancestor_id"),
                KnowledgeNode.parent_id.label("ancestor_parent_id"),
                KnowledgeNode.docs_library_id.label("docs_library_id"),
                KnowledgeNode.system_key.label("system_key"),
            )
            .where(KnowledgeNode.docs_library_id == docs_library_id)
            .cte(f"docs_query_email_ancestors_{docs_library_id.hex}", recursive=True)
        )
        parent = aliased(KnowledgeNode)
        ancestors = ancestors.union(
            select(
                ancestors.c.descendant_id,
                parent.id,
                parent.parent_id,
                parent.docs_library_id,
                parent.system_key,
            ).join(
                parent,
                and_(
                    parent.id == ancestors.c.ancestor_parent_id,
                    # The defensive traversal records the first foreign parent
                    # for tag checking, then stops at that library boundary.
                    ancestors.c.docs_library_id == docs_library_id,
                ),
            )
        )
        email_tag = (
            select(literal(1))
            .select_from(KnowledgeNodeSupertag)
            .join(
                KnowledgeSupertag,
                KnowledgeSupertag.id == KnowledgeNodeSupertag.supertag_id,
            )
            .where(
                KnowledgeNodeSupertag.node_id == ancestors.c.ancestor_id,
                KnowledgeSupertag.docs_library_id == docs_library_id,
                KnowledgeSupertag.system_key == "email",
            )
            .exists()
        )
        is_email_tree = (
            select(literal(1))
            .select_from(ancestors)
            .where(
                ancestors.c.descendant_id == KnowledgeNode.id,
                or_(
                    and_(
                        ancestors.c.docs_library_id == docs_library_id,
                        func.substr(
                            func.btrim(ancestors.c.system_key, _QUERY_STRIP_CHARS), 1, 12
                        ) == "project_mail",
                    ),
                    email_tag,
                ),
            )
            .exists()
        )
        return stmt.where(
            or_(
                KnowledgeNode.project_id.is_(None),
                KnowledgeNode.project_id == normalized_project_id,
                ~is_email_tree,
            )
        )

    async def _split_task_field_filters(
        self,
        *,
        docs_library_id: uuid.UUID,
        field_filters: dict[str, str] | None,
    ) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
        """Separate SQL-safe, synthetic, and typed Docs field filters.

        Ordinary ASCII text uses the shared SQL scalar. Typed, synthetic,
        legacy JSON, and Unicode values use canonical Python resolution
        (including reference ACL checks), as does their grouping.
        """

        filters = {
            str(name): str(value)
            for name, value in (field_filters or {}).items()
            if str(name).strip()
        }
        if not filters:
            return {}, {}, {}
        requested = {name.strip().casefold() for name in filters}
        field_rows = await self._query_field_rows([docs_library_id], requested)
        task_references: set[str] = set()
        typed_references: set[str] = set()
        for name, system_key, field_type, python_value in field_rows:
            normalized_name = str(name or "").strip().casefold()
            normalized_system_key = str(system_key or "").casefold()
            if normalized_system_key in TASK_FIELD_TO_TASK_UPDATE:
                task_references.add(normalized_name)
                task_references.add(normalized_system_key)
            elif str(field_type or "text").casefold() != "text" or python_value:
                typed_references.add(normalized_name)
                typed_references.add(normalized_system_key)
        native: dict[str, str] = {}
        task: dict[str, str] = {}
        typed: dict[str, str] = {}
        for name, value in filters.items():
            normalized_name = name.strip().casefold()
            if normalized_name in task_references:
                # Keep the caller's alias so two aliases for one task field
                # remain independent AND predicates instead of overwriting
                # each other after canonicalization.
                task[name] = value
            elif normalized_name in typed_references:
                typed[name] = value
            else:
                native[name] = value
        return native, task, typed

    async def _build_structured_query_statement(
        self,
        *,
        docs_library_id: uuid.UUID | None = None,
        workspace_id: uuid.UUID | None = None,
        tags: list[str] | None = None,
        text: str = "",
        project_id: uuid.UUID | None = None,
        field_filters: dict[str, str] | None = None,
        user_id: uuid.UUID | None = None,
        node_ids: Iterable[uuid.UUID] | None = None,
        turn_project_id: uuid.UUID | None = None,
    ) -> tuple[Any, Any]:
        """Build one ACL-filtered structured-query relation.

        Callers use this relation twice: once for exact metadata and once for
        the bounded page.  Keeping all predicates in this shared builder is
        what prevents the count query from drifting from the returned rows.
        """

        resolved_library_id = _resolve_docs_library_id(docs_library_id, workspace_id)
        stmt = select(KnowledgeNode).where(
            KnowledgeNode.docs_library_id == resolved_library_id,
            KnowledgeNode.archived_at.is_(None),
            docs_node_renderable_predicate(KnowledgeNode),
        )
        shared_nodes = None
        library_row = None
        if user_id is not None:
            library_row = await self.session.get(DocsLibrary, resolved_library_id)
            actor = _coerce_uuid(user_id)
            if actor is not None and getattr(library_row, "owner_user_id", None) != actor:
                shared_nodes = _shared_nodes_cte(
                    docs_library_id=resolved_library_id,
                    user_id=actor,
                    name=f"docs_query_shared_nodes_{resolved_library_id.hex}",
                )
            stmt = apply_docs_visibility(
                stmt,
                docs_library_id=resolved_library_id,
                user_id=user_id,
                node_model=KnowledgeNode,
                library_owner_id=getattr(library_row, "owner_user_id", None),
                shared_nodes=shared_nodes,
            )
        if node_ids is not None:
            normalized_node_ids = [
                node_id
                for raw_node_id in node_ids
                if (node_id := _coerce_uuid(raw_node_id)) is not None
            ]
            stmt = stmt.where(docs_id_predicate(KnowledgeNode.id, normalized_node_ids, self.session))
        if project_id is not None:
            stmt = stmt.where(KnowledgeNode.project_id == project_id)
        if text.strip():
            stmt = stmt.where(KnowledgeNode.title.ilike(f"%{text.strip()}%"))
        for tag_name in tags or []:
            tag_name = str(tag_name).strip().lstrip("#")
            if not tag_name:
                continue
            try:
                tag_row = await self.resolve_supertag(
                    docs_library_id=resolved_library_id, tag=tag_name, create=False
                )
            except ValueError as exc:
                if not str(exc).startswith("supertag not found:"):
                    raise
                # Tags are local to a library; only this relation is empty.
                stmt = stmt.where(KnowledgeNode.id.in_([]))
                continue
            tag_exists = (
                select(KnowledgeNodeSupertag.node_id)
                .select_from(KnowledgeNodeSupertag)
                .join(
                    KnowledgeSupertag,
                    KnowledgeSupertag.id == KnowledgeNodeSupertag.supertag_id,
                )
                .where(
                    KnowledgeNodeSupertag.node_id == KnowledgeNode.id,
                    KnowledgeNodeSupertag.supertag_id == tag_row.id,
                    KnowledgeSupertag.id == tag_row.id,
                    KnowledgeSupertag.docs_library_id == resolved_library_id,
                )
                .exists()
            )
            stmt = stmt.where(tag_exists)
        for field_name, expected in (field_filters or {}).items():
            field_name = str(field_name).strip()
            if not field_name:
                continue
            stmt = stmt.where(
                func.lower(self._query_text_value(
                    KnowledgeNode.id, KnowledgeNode.docs_library_id, field_name
                )) == str(expected).strip().casefold()
            )
        stmt = self._query_email_turn_visibility(
            stmt,
            docs_library_id=resolved_library_id,
            turn_project_id=turn_project_id,
        )
        return stmt, library_row

    @staticmethod
    def _lookup_group_value(values: dict[str, Any], group_by: str) -> str:
        requested = str(group_by or "").strip().casefold()
        for name, value in values.items():
            if str(name).casefold() == requested:
                return str(value or "")
        return ""

    async def _node_group_value(
        self,
        node: KnowledgeNode,
        *,
        group_by: str,
        user_id: uuid.UUID | None,
        turn_project_id: uuid.UUID | None = None,
    ) -> str:
        """Resolve a group value by either field name or system key.

        Both typed aggregation and bounded-row rendering use the canonical
        field reference map, which retains identity for colliding names.
        """

        values = await self._canonical_query_fields(
            node, user_id=user_id, turn_project_id=turn_project_id
        )
        entry = values.get(str(group_by or "").strip().casefold())
        return entry[1] if entry else ""

    @staticmethod
    def _query_python_value_exists() -> Any:
        # Legacy text values may live in JSON. Python str(dict/list) is not a
        # database JSON serialization. Unicode casefold is also not SQL lower;
        # non-ASCII text values use Python comparison regardless of DB locale.
        return select(literal(1)).select_from(KnowledgeFieldValue).where(
            KnowledgeFieldValue.field_id == KnowledgeField.id,
            or_(
                and_(
                    KnowledgeFieldValue.value_text.is_(None),
                    KnowledgeFieldValue.value_json.is_not(None),
                    cast(KnowledgeFieldValue.value_json, String) != "null",
                ),
                func.octet_length(KnowledgeFieldValue.value_text)
                != func.length(KnowledgeFieldValue.value_text),
            ),
        ).correlate(KnowledgeField).exists()

    async def _query_field_rows(
        self, library_ids: Iterable[uuid.UUID], requested: set[str]
    ) -> list[tuple[str, str | None, str, bool]]:
        # Discover definitions with the same normalization as the canonical
        # resolver. SQL lower(name) would lose e.g. Straße when asked for STRASSE.
        result = await self.session.execute(select(
            KnowledgeField.name, KnowledgeField.system_key, KnowledgeField.field_type,
            self._query_python_value_exists(),
        ).where(KnowledgeField.docs_library_id.in_(list(library_ids))))
        return [
            (name, key, kind, bool(python_value or not name.isascii() or not (key or "").isascii()))
            for name, key, kind, python_value in result.all()
            if name.casefold() in requested or (key or "").casefold() in requested
        ]

    @staticmethod
    def _query_text_value(node_id: Any, library_id: Any, requested: str) -> Any:
        """SQL counterpart of _canonical_query_fields for ordinary text.

        Ignore empty values, prefer a populated system alias over a display
        name, then choose the last field in stable field order. Filters and
        aggregates must use this same scalar expression.
        """
        wanted = requested.strip().casefold()
        alias_match = func.lower(KnowledgeField.system_key) == wanted
        return (
            select(KnowledgeFieldValue.value_text)
            .join(KnowledgeField, KnowledgeField.id == KnowledgeFieldValue.field_id)
            .where(
                KnowledgeFieldValue.node_id == node_id,
                KnowledgeField.docs_library_id == library_id,
                or_(func.lower(KnowledgeField.name) == wanted, alias_match),
                KnowledgeFieldValue.value_text.is_not(None),
                KnowledgeFieldValue.value_text != "",
            )
            .order_by(
                case((alias_match, 1), else_=0).desc(),
                KnowledgeField.sort_order.desc(),
                KnowledgeField.created_at.desc().nulls_last(),
                KnowledgeField.id.desc(),
            )
            .limit(1)
            .correlate_except(KnowledgeField, KnowledgeFieldValue)
            .scalar_subquery()
        )

    async def _canonical_query_fields(
        self, node: KnowledgeNode, *, user_id: uuid.UUID | None,
        turn_project_id: uuid.UUID | None = None,
    ) -> dict[str, tuple[KnowledgeField, str]]:
        """Resolve populated field references without discarding field IDs.

        Task values replace only their own field ID. Duplicate display names
        select the last populated field; stable system aliases take precedence
        over display names. The order matches _query_text_value.
        """
        result = await self.session.execute(
            select(KnowledgeField, KnowledgeFieldValue)
            .join(KnowledgeFieldValue, KnowledgeFieldValue.field_id == KnowledgeField.id)
            .where(KnowledgeFieldValue.node_id == node.id,
                   KnowledgeField.docs_library_id == node.docs_library_id)
        )
        entries: dict[uuid.UUID, tuple[KnowledgeField, str]] = {}
        for field, value in result.all():
            if (field.field_type == "reference" and value.target_node_id is not None
                    and not await self._query_reference_visible(
                        value.target_node_id, user_id=user_id, turn_project_id=turn_project_id
                    )):
                continue
            entries[field.id] = (field, self._format_field_value(field, value))
        task = await self._get_bound_task(node)
        if task is not None and await self._can_read_bound_task_metadata(
            node=node, task=task, user_id=user_id
        ):
            definitions = await self.session.execute(
                select(KnowledgeField)
                .join(KnowledgeNodeSupertag,
                      KnowledgeNodeSupertag.supertag_id == KnowledgeField.supertag_id)
                .where(
                    KnowledgeNodeSupertag.node_id == node.id,
                    KnowledgeField.docs_library_id == node.docs_library_id,
                    func.lower(KnowledgeField.system_key).in_(TASK_FIELD_TO_TASK_UPDATE),
                )
            )
            for field in definitions.scalars().unique().all():
                task_attr = TASK_FIELD_TO_TASK_UPDATE[field.system_key.casefold()]
                raw = getattr(task, task_attr, None)
                if raw not in (None, ""):
                    entries[field.id] = (
                        field, raw.isoformat() if isinstance(raw, datetime) else str(raw)
                    )
        names: dict[str, tuple[KnowledgeField, str]] = {}
        aliases: dict[str, tuple[KnowledgeField, str]] = {}
        for field, value in sorted(entries.values(), key=lambda entry: (
            entry[0].sort_order or 0, entry[0].created_at or datetime.min, entry[0].id
        )):
            if value == "":
                continue
            names[field.name.casefold()] = (field, value)
            if field.system_key:
                aliases[field.system_key.casefold()] = (field, value)
        return {**names, **aliases}

    @staticmethod
    def _field_filter_value_matches(
        actual: Any,
        expected: Any,
        *,
        field_type: str = "text",
    ) -> bool:
        """Compare the user-facing field value without SQL wildcard leaks."""

        actual_text = str(actual or "")
        expected_text = str(expected or "").strip()
        normalized_type = str(field_type or "text").casefold()
        if normalized_type == "number":
            try:
                return Decimal(actual_text) == Decimal(expected_text)
            except (InvalidOperation, ValueError):
                return actual_text.casefold() == expected_text.casefold()
        if normalized_type == "reference":
            if actual_text.casefold() == expected_text.casefold():
                return True
            match = re.fullmatch(r"\[\[node:([^\]]+)\]\]", actual_text.strip(), re.I)
            if match is None:
                return False
            target_text = match.group(1).replace("-", "").casefold()
            compact_expected = expected_text.replace("-", "").casefold()
            # Preserve the historical short-UUID reference filter, but only
            # for a safe hexadecimal prefix.  Arbitrary input (including `%`
            # or `_`) must never become a SQL LIKE pattern.
            return bool(
                re.fullmatch(r"[0-9a-f]{8,32}", compact_expected)
                and target_text.startswith(compact_expected)
            )
        return actual_text.casefold() == expected_text.casefold()

    async def _node_matches_field_filters(
        self,
        node: KnowledgeNode,
        field_filters: dict[str, str],
        *,
        user_id: uuid.UUID | None,
        turn_project_id: uuid.UUID | None = None,
    ) -> bool:
        """Apply canonical rendered-field equality to a candidate node."""

        if not field_filters:
            return True
        values = await self._canonical_query_fields(
            node, user_id=user_id, turn_project_id=turn_project_id
        )
        for name, expected in field_filters.items():
            requested = str(name or "").strip().casefold()
            entry = values.get(requested)
            if entry is None:
                return False
            field, actual = entry
            if not self._field_filter_value_matches(
                actual,
                expected,
                field_type=getattr(field, "field_type", "text"),
            ):
                return False
        return True

    async def _query_group_counts(
        self,
        stmt: Any,
        *,
        docs_library_id: uuid.UUID | Iterable[uuid.UUID],
        group_by: str,
        total_matches: int,
        user_id: uuid.UUID | None,
        turn_project_id: uuid.UUID | None = None,
    ) -> dict[str, int]:
        """Return exact group totals from the ACL-filtered candidate relation.

        Plain text fields use a correlated SQL aggregate.  Typed fields,
        reference fields, and synthetic Task fields fall back to the existing
        value renderer so their display/ACL semantics remain identical to
        ``get_node_field_values``.
        """

        requested = str(group_by or "").strip()
        if not requested:
            return {}
        field_rows = await self._query_field_rows(
            [docs_library_id] if isinstance(docs_library_id, uuid.UUID) else docs_library_id,
            {requested.casefold()},
        )
        field_types = {str(row[2] or "text").casefold() for row in field_rows}
        field_system_keys = {str(row[1] or "").casefold() for row in field_rows}
        sql_text_group = bool(field_rows) and field_types == {"text"}
        if any(row[3] for row in field_rows):
            sql_text_group = False
        if any(key in TASK_FIELD_TO_TASK_UPDATE for key in field_system_keys):
            sql_text_group = False

        if sql_text_group:
            candidates = (
                stmt.order_by(None)
                .with_only_columns(
                    KnowledgeNode.id, KnowledgeNode.docs_library_id, maintain_column_froms=True
                )
                .subquery("docs_query_group_candidates")
            )
            value_subquery = self._query_text_value(
                candidates.c.id, candidates.c.docs_library_id, requested
            )
            group_key = func.coalesce(
                func.nullif(value_subquery, ""), literal("(none)")
            )
            grouped = await self.session.execute(
                select(group_key.label("group_key"), func.count())
                .select_from(candidates)
                .group_by(group_key)
            )
            return {
                str(group_key_value or "(none)"): int(count or 0)
                for group_key_value, count in grouped.all()
            }

        if not field_rows:
            return {"(none)": total_matches} if total_matches else {}

        # Typed/reference/task fields need the canonical Python renderer to
        # preserve date/checkbox/reference formatting and target ACL checks.
        counts: dict[str, int] = {}
        async for node in self._query_candidate_batches(stmt):
            key = await self._node_group_value(
                node,
                group_by=requested,
                user_id=user_id,
                turn_project_id=turn_project_id,
            ) or "(none)"
            counts[key] = counts.get(key, 0) + 1
        return counts

    async def _group_counts_from_nodes(
        self,
        nodes: Iterable[KnowledgeNode],
        *,
        group_by: str,
        user_id: uuid.UUID | None,
        turn_project_id: uuid.UUID | None = None,
    ) -> dict[str, int]:
        """Group an already exact, ACL-filtered node set via canonical values."""

        requested = str(group_by or "").strip()
        if not requested:
            return {}
        counts: dict[str, int] = {}
        for node in nodes:
            key = await self._node_group_value(
                node,
                group_by=requested,
                user_id=user_id,
                turn_project_id=turn_project_id,
            ) or "(none)"
            counts[key] = counts.get(key, 0) + 1
        return counts

    async def query_nodes_result(
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
        node_ids: Iterable[uuid.UUID] | None = None,
        group_by: str = "",
        turn_project_id: uuid.UUID | None = None,
        date_from: str = "",
        date_to: str = "",
        order_by: str = "updated_at",
        order: str = "desc",
        offset: int = 0,
    ) -> DocsQueryResult:
        """Run a structured query with exact ACL-filtered metadata."""

        resolved_library_id = _resolve_docs_library_id(docs_library_id, workspace_id)
        (
            native_field_filters,
            task_field_filters,
            typed_field_filters,
        ) = await self._split_task_field_filters(
            docs_library_id=resolved_library_id,
            field_filters=field_filters,
        )
        stmt, _library_row = await self._build_structured_query_statement(
            docs_library_id=resolved_library_id,
            tags=tags,
            text=text,
            project_id=project_id,
            field_filters=native_field_filters,
            user_id=user_id,
            node_ids=node_ids,
            turn_project_id=turn_project_id,
        )
        return await self._execute_query_result(
            stmt, library_ids=[resolved_library_id],
            canonical_field_filters={**typed_field_filters, **task_field_filters},
            turn_project_id=turn_project_id,
            group_by=group_by, limit=limit, user_id=user_id,
            date_from=date_from, date_to=date_to, order_by=order_by, order=order, offset=offset,
        )

    @staticmethod
    def _query_window(
        stmt: Any, *, date_from: str, date_to: str, order_by: str, order: str, offset: int
    ) -> Any:
        """Closed timeline controls; date bounds apply to the ordering field.

        Date-only upper bounds include the entire day. Timestamp bounds are
        inclusive instants, normalized to UTC for the naive DB timestamps.
        NULLs sort last in either direction; UUID ascending breaks all ties.
        """
        if not isinstance(order_by, str) or order_by not in {"updated_at", "created_at", "day_date"}:
            raise ValueError("order_by must be updated_at, created_at or day_date")
        if not isinstance(order, str) or order not in {"asc", "desc"}:
            raise ValueError("order must be asc or desc")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        column = getattr(KnowledgeNode, order_by)
        bounds: list[date | datetime | None] = []
        upper_exclusive = False
        for index, raw in enumerate((date_from, date_to)):
            value = str(raw or "").strip()
            if not value:
                bounds.append(None)
                continue
            if order_by == "day_date":
                bound = date.fromisoformat(value)
            else:
                bound = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if bound.tzinfo is not None:
                    bound = bound.astimezone(timezone.utc).replace(tzinfo=None)
                if index == 1 and len(value) == 10:
                    bound += timedelta(days=1)
                    upper_exclusive = True
            bounds.append(bound)
        lower, upper = bounds
        if lower is not None and upper is not None and (
            lower > upper or (upper_exclusive and lower == upper)
        ):
            raise ValueError("date_from must not follow date_to")
        if lower is not None:
            stmt = stmt.where(column >= lower)
        if upper is not None:
            stmt = stmt.where(column < upper if upper_exclusive else column <= upper)
        return stmt.order_by(None).order_by(
            (column.asc() if order == "asc" else column.desc()).nulls_last(),
            KnowledgeNode.id.asc(),
        )

    async def _query_candidate_batches(self, stmt: Any) -> AsyncIterator[KnowledgeNode]:
        """Bound materialization for fields that require Python rendering."""
        position = 0
        ordered = stmt.order_by(KnowledgeNode.id.asc())
        while True:
            result = await self.session.execute(ordered.limit(200).offset(position))
            nodes = list(result.scalars().unique().all())
            for node in nodes:
                yield node
            if len(nodes) < 200:
                break
            position += len(nodes)

    async def _execute_query_result(
        self, stmt: Any, *, library_ids: list[uuid.UUID],
        canonical_field_filters: dict[str, str], group_by: str,
        limit: int, user_id: uuid.UUID | None, date_from: str, date_to: str,
        order_by: str, order: str, offset: int,
        canonical_filters_by_library: dict[uuid.UUID, dict[str, str]] | None = None,
        turn_project_id: uuid.UUID | None = None,
    ) -> DocsQueryResult:
        stmt = self._query_window(
            stmt, date_from=date_from, date_to=date_to,
            order_by=order_by, order=order, offset=offset,
        )
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        bounded_limit = min(limit, 200)
        per_library_filters = canonical_filters_by_library or {}
        if canonical_field_filters or any(per_library_filters.values()):
            # Task values are synthetic fields backed by a separate table, and
            # typed/reference values need their canonical renderer (including
            # reference ACL checks).  Resolve them after the shared SQL
            # candidate relation has enforced all stored Docs predicates; this
            # keeps the exact count and bounded page on the same ACL set.
            nodes: list[KnowledgeNode] = []
            total_matches = 0
            group_counts: dict[str, int] = {}
            async for node in self._query_candidate_batches(stmt):
                if await self._node_matches_field_filters(
                    node,
                    {**canonical_field_filters, **per_library_filters.get(node.docs_library_id, {})},
                    user_id=user_id,
                    turn_project_id=turn_project_id,
                ):
                    if offset <= total_matches < offset + bounded_limit:
                        nodes.append(node)
                    total_matches += 1
                    if group_by.strip():
                        key = await self._node_group_value(
                            node, group_by=group_by, user_id=user_id, turn_project_id=turn_project_id
                        ) or "(none)"
                        group_counts[key] = group_counts.get(key, 0) + 1
        else:
            count_candidates = (
                stmt.order_by(None)
                .with_only_columns(KnowledgeNode.id, maintain_column_froms=True)
                .subquery("docs_query_count_candidates")
            )
            total_result = await self.session.execute(
                select(func.count()).select_from(count_candidates)
            )
            total_matches = int(total_result.scalar_one() or 0)
            rows_result = await self.session.execute(
                stmt.limit(bounded_limit).offset(offset)
            )
            nodes = list(rows_result.scalars().unique().all())
            group_counts = await self._query_group_counts(
                stmt,
                docs_library_id=library_ids,
                group_by=group_by,
                total_matches=total_matches,
                user_id=user_id,
                turn_project_id=turn_project_id,
            )
        returned = len(nodes)
        has_more = total_matches > offset + returned
        return DocsQueryResult(
            nodes=nodes,
            total_matches=total_matches,
            returned=returned,
            truncated=total_matches > returned,
            has_more=has_more,
            group_counts=group_counts,
            offset=offset,
        )

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
        result = await self.query_nodes_result(
            docs_library_id=docs_library_id,
            workspace_id=workspace_id,
            tags=tags,
            text=text,
            project_id=project_id,
            field_filters=field_filters,
            limit=limit,
            user_id=user_id,
        )
        return result.nodes

    async def query_with_scope_result(
        self,
        *,
        docs_scope: DocsScope,
        tags: list[str] | None = None,
        text: str | None = None,
        field_filters: dict[str, str] | None = None,
        group_by: str = "",
        limit: int = 20,
        user_id: uuid.UUID | None = None,
        turn_project_id: uuid.UUID | None = None,
        date_from: str = "",
        date_to: str = "",
        order_by: str = "updated_at",
        order: str = "desc",
        offset: int = 0,
    ) -> DocsQueryResult:
        """Run a scoped structured query with exact pre-limit aggregates."""

        # Build per-library authorized relations, then order/count/page their
        # union in SQL. No per-library page is discarded before global paging.
        library_ids = list(dict.fromkeys(
            value for raw in docs_scope.allowed_library_ids
            if (value := _coerce_uuid(raw)) is not None
        ))
        canonical_ids = {
            node_id
            for raw_node_id in docs_scope.canonical_node_ids
            if (node_id := _coerce_uuid(raw_node_id)) is not None
        }
        related_ids = {
            node_id
            for raw_node_id in docs_scope.related_node_ids
            if (node_id := _coerce_uuid(raw_node_id)) is not None
        }
        scoped_node_ids = canonical_ids | related_ids
        if not library_ids or not scoped_node_ids:
            self._query_window(
                select(KnowledgeNode), date_from=date_from, date_to=date_to,
                order_by=order_by, order=order, offset=offset,
            )
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                raise ValueError("limit must be a positive integer")
            return DocsQueryResult([], 0, 0, False, False, {}, offset=offset)
        relations = []
        canonical_filters_by_library: dict[uuid.UUID, dict[str, str]] = {}
        for library_id in library_ids:
            native, task, typed = await self._split_task_field_filters(
                docs_library_id=library_id, field_filters=field_filters
            )
            # The same reference may denote a number, text or synthetic task
            # field in different libraries. Keep its execution plan local:
            # SQL applies only this library's native predicates, and Python
            # applies only its remaining predicates after the scoped union.
            canonical_filters_by_library[library_id] = {**task, **typed}
            relation, _ = await self._build_structured_query_statement(
                docs_library_id=library_id,
                tags=tags,
                text=text or "",
                field_filters=native,
                user_id=user_id,
                node_ids=scoped_node_ids,
                turn_project_id=turn_project_id,
            )
            relations.append(relation.with_only_columns(
                KnowledgeNode.id, maintain_column_froms=True
            ))
        stmt = select(KnowledgeNode).where(
            KnowledgeNode.id.in_(union_all(*relations)) if relations
            else KnowledgeNode.id.in_([])
        )
        return await self._execute_query_result(
            stmt, library_ids=library_ids, canonical_field_filters={},
            canonical_filters_by_library=canonical_filters_by_library,
            turn_project_id=turn_project_id,
            group_by=group_by, limit=limit, user_id=user_id,
            date_from=date_from, date_to=date_to, order_by=order_by, order=order, offset=offset,
        )

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
