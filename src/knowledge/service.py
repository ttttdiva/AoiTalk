"""Knowledge Workspace service layer.

External files are treated as the canonical source. Database rows and vector
payloads are derived manifest/index data that can be rebuilt from files.
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import logging
import mimetypes
import re
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml
from sqlalchemy import JSON, String, and_, any_, case, cast, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..memory.models import (
    KnowledgeAnnotation,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeEditEvent,
    KnowledgeLink,
    KnowledgeSource,
    KnowledgeSourcePermission,
)
from .growi_client import build_page_url
from . import document_dates
from .storage_scan import scan_source, read_source_file, verify_source_online, source_io_key
from ..services.storage_io import StorageError, storage_io
from ..services.storage_roots import lexical_path, relative_parts


TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".xml", ".log"}
OFFICE_EXTENSIONS = {".docx", ".xlsx", ".pptx", ".pdf"}
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | OFFICE_EXTENSIONS
DEFAULT_INCLUDE_PATTERNS = ["*.md", "*.txt", "*.pdf", "*.docx", "*.xlsx", "*.pptx"]
DEFAULT_EXCLUDE_PATTERNS = [".*", "__pycache__", "node_modules", ".git"]
# GROWI 取り込みで既定除外する Wiki パス（ゴミ箱・個人ページ）。
DEFAULT_GROWI_EXCLUDE_PATTERNS = ["/trash/*", "/trash", "/user/*"]

DOCUMENT_DATE_FRONTMATTER_KEYS = document_dates.DOCUMENT_DATE_FRONTMATTER_KEYS
GROWI_DOCUMENT_DATE_KEYS = document_dates.GROWI_DOCUMENT_DATE_KEYS
KNOWLEDGE_DOCUMENT_DATE_SOURCES = document_dates.KNOWLEDGE_DOCUMENT_DATE_SOURCES
# A write/owner source grant is also sufficient to read its contents.  Keep
# this closed so an unrelated or malformed permission value never becomes an
# implicit Knowledge read grant.
KNOWLEDGE_SOURCE_READ_PERMISSIONS = frozenset({"read", "write", "owner"})
KNOWLEDGE_DOCUMENT_STATUSES = frozenset({"active", "error", "deleted", "inactive"})
KNOWLEDGE_QUERY_OPERATIONS = frozenset({"count", "list", "group"})
KNOWLEDGE_QUERY_GROUP_FIELDS = frozenset(
    {"source_id", "source", "project_id", "extension", "tag", "status", "date_source"}
)
KNOWLEDGE_QUERY_ORDER_FIELDS = frozenset({"date", "document_date"})
KNOWLEDGE_QUERY_ORDER_DIRECTIONS = frozenset({"asc", "desc"})
KNOWLEDGE_QUERY_MAX_LIMIT = 100

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_WIKI_LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
_MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KnowledgeSearchFilters:
    source_id: Optional[uuid.UUID] = None
    project_id: Optional[uuid.UUID] = None
    tags: tuple[str, ...] = ()
    extension: Optional[str] = None
    path_prefix: Optional[str] = None
    # Server-resolved only: never exposed as a model/API argument.
    readable_source_ids: Optional[tuple[uuid.UUID, ...]] = None


@dataclass(frozen=True)
class KnowledgeQueryFilters:
    """Allowlisted filters for exact/count/list Knowledge queries.

    ``document_date`` is the effective UTC date derived during ingestion.  A
    legacy row whose timezone cannot be recovered remains unknown until
    source synchronization establishes a current, proven date.
    """

    source_id: Optional[uuid.UUID | str] = None
    project_id: Optional[uuid.UUID | str] = None
    tags: tuple[str, ...] = ()
    extension: Optional[str] = None
    path_prefix: Optional[str] = None
    status: Optional[str] = "active"
    date_from: Optional[date | datetime | str] = None
    date_to: Optional[date | datetime | str] = None


class KnowledgeService:
    """Coordinates sources, manifests, search, and organizer annotations."""

    @staticmethod
    def normalize_patterns(patterns: Optional[Iterable[str]], defaults: list[str]) -> list[str]:
        normalized = [str(pattern).strip() for pattern in patterns or [] if str(pattern).strip()]
        return normalized or list(defaults)

    @staticmethod
    def resolve_root_path(root_path: str) -> Path:
        root = Path(root_path).expanduser().resolve()
        if not root.exists():
            raise ValueError("ナレッジソースのパスが存在しません")
        if not root.is_dir():
            raise ValueError("ナレッジソースのパスはディレクトリである必要があります")
        return root

    @staticmethod
    def _coerce_uuid(value: uuid.UUID | str | None) -> Optional[uuid.UUID]:
        if value is None or isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))

    @staticmethod
    def _is_admin(user_info: Optional[dict[str, Any]]) -> bool:
        return bool(user_info and user_info.get("role") == "admin")

    @staticmethod
    def _actor_id(user_info: Optional[dict[str, Any]]) -> Optional[uuid.UUID]:
        if not user_info or not user_info.get("id"):
            return None
        return uuid.UUID(str(user_info["id"]))

    @staticmethod
    async def create_source(
        session: AsyncSession,
        *,
        actor_user_id: uuid.UUID,
        name: str,
        root_path: str,
        description: Optional[str] = None,
        source_type: str = "local_dir",
        include_patterns: Optional[list[str]] = None,
        exclude_patterns: Optional[list[str]] = None,
        sync_mode: str = "manual",
        write_policy: str = "propose_patch",
        access_policy: Optional[dict[str, Any]] = None,
    ) -> KnowledgeSource:
        root = await storage_io.run(
            source_io_key(root_path), lambda: KnowledgeService.resolve_root_path(root_path),
            timeout=3,
        )
        source = KnowledgeSource(
            name=name.strip(),
            description=description,
            root_path=str(root),
            source_type=source_type,
            owner_user_id=actor_user_id,
            access_policy=access_policy or {},
            include_patterns=KnowledgeService.normalize_patterns(
                include_patterns, DEFAULT_INCLUDE_PATTERNS
            ),
            exclude_patterns=KnowledgeService.normalize_patterns(
                exclude_patterns, DEFAULT_EXCLUDE_PATTERNS
            ),
            sync_mode=sync_mode,
            write_policy=write_policy,
            status="created",
            document_count=0,
            chunk_count=0,
        )
        session.add(source)
        await session.flush()
        session.add(
            KnowledgeSourcePermission(
                source_id=source.id,
                user_id=actor_user_id,
                permission="write",
                created_by=actor_user_id,
            )
        )
        return source

    @staticmethod
    async def create_growi_source(
        session: AsyncSession,
        *,
        actor_user_id: uuid.UUID,
        name: str,
        base_url: str,
        api_token: str,
        description: Optional[str] = None,
        exclude_patterns: Optional[list[str]] = None,
        sync_mode: str = "manual",
        access_policy: Optional[dict[str, Any]] = None,
    ) -> KnowledgeSource:
        """GROWI 社内Wiki を取り込む Knowledge Source を作成する。

        root_path には GROWI のベースURLを保持し、API トークンは暗号化列に保存する。
        """
        base = (base_url or "").strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise ValueError("GROWI のURLは http:// または https:// で始まる必要があります")
        if not (api_token or "").strip():
            raise ValueError("GROWI の API トークンが必要です")
        source = KnowledgeSource(
            name=(name or "GROWI Wiki").strip(),
            description=description,
            root_path=base,
            source_type="growi",
            owner_user_id=actor_user_id,
            access_policy=access_policy or {},
            include_patterns=["*"],
            exclude_patterns=KnowledgeService.normalize_patterns(
                exclude_patterns, DEFAULT_GROWI_EXCLUDE_PATTERNS
            ),
            sync_mode=sync_mode,
            write_policy="read_only",
            status="created",
            document_count=0,
            chunk_count=0,
        )
        source.growi_api_token = api_token.strip()
        session.add(source)
        await session.flush()
        session.add(
            KnowledgeSourcePermission(
                source_id=source.id,
                user_id=actor_user_id,
                permission="write",
                created_by=actor_user_id,
            )
        )
        return source

    @staticmethod
    async def create_project_workspace_source(
        session: AsyncSession,
        *,
        actor_user_id: uuid.UUID,
        project_id: uuid.UUID,
        root_path: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        include_patterns: Optional[list[str]] = None,
        exclude_patterns: Optional[list[str]] = None,
        sync_mode: str = "manual",
        write_policy: str = "propose_patch",
    ) -> KnowledgeSource:
        """Create or return the managed Knowledge Source for a project workspace."""
        root = await storage_io.run(
            source_io_key(root_path), lambda: KnowledgeService.resolve_root_path(root_path),
            timeout=3,
        )
        existing_result = await session.execute(
            select(KnowledgeSource).where(KnowledgeSource.source_type == "project_workspace")
        )
        for source in existing_result.scalars().all():
            policy = source.access_policy or {}
            if str(policy.get("project_id") or "") == str(project_id):
                source.root_path = str(root)
                if name:
                    source.name = name.strip()
                if description is not None:
                    source.description = description
                source.updated_at = datetime.utcnow()
                await KnowledgeService._ensure_project_source_permission(
                    session,
                    source_id=source.id,
                    project_id=project_id,
                    actor_user_id=actor_user_id,
                )
                return source

        source = KnowledgeSource(
            name=(name or "Project Workspace").strip(),
            description=description,
            root_path=str(root),
            source_type="project_workspace",
            owner_user_id=actor_user_id,
            access_policy={"managed": True, "project_id": str(project_id)},
            include_patterns=KnowledgeService.normalize_patterns(
                include_patterns, DEFAULT_INCLUDE_PATTERNS
            ),
            exclude_patterns=KnowledgeService.normalize_patterns(
                exclude_patterns, DEFAULT_EXCLUDE_PATTERNS
            ),
            sync_mode=sync_mode,
            write_policy=write_policy,
            status="created",
            document_count=0,
            chunk_count=0,
        )
        session.add(source)
        await session.flush()
        session.add(
            KnowledgeSourcePermission(
                source_id=source.id,
                user_id=actor_user_id,
                permission="write",
                created_by=actor_user_id,
            )
        )
        await KnowledgeService._ensure_project_source_permission(
            session,
            source_id=source.id,
            project_id=project_id,
            actor_user_id=actor_user_id,
        )
        return source

    @staticmethod
    async def _ensure_project_source_permission(
        session: AsyncSession,
        *,
        source_id: uuid.UUID,
        project_id: uuid.UUID,
        actor_user_id: uuid.UUID,
    ) -> None:
        existing = await session.execute(
            select(KnowledgeSourcePermission.id)
            .where(
                KnowledgeSourcePermission.source_id == source_id,
                KnowledgeSourcePermission.project_id == project_id,
            )
            .limit(1)
        )
        if existing.scalar_one_or_none():
            return
        session.add(
            KnowledgeSourcePermission(
                source_id=source_id,
                project_id=project_id,
                permission="write",
                created_by=actor_user_id,
            )
        )

    @staticmethod
    async def get_source(
        session: AsyncSession,
        source_id: uuid.UUID | str,
    ) -> Optional[KnowledgeSource]:
        result = await session.execute(
            select(KnowledgeSource)
            .options(selectinload(KnowledgeSource.permissions))
            .where(KnowledgeSource.id == KnowledgeService._coerce_uuid(source_id))
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def can_read_source(
        session: AsyncSession,
        *,
        source_id: uuid.UUID,
        actor_user_id: Optional[uuid.UUID],
        is_admin: bool = False,
    ) -> bool:
        if is_admin:
            return True
        if actor_user_id is None:
            return False
        source = await KnowledgeService.get_source(session, source_id)
        if not source:
            return False
        if source.owner_user_id == actor_user_id:
            return True
        user_perm = await session.execute(
            select(KnowledgeSourcePermission.id)
            .where(
                KnowledgeSourcePermission.source_id == source_id,
                KnowledgeSourcePermission.user_id == actor_user_id,
                KnowledgeSourcePermission.permission.in_(
                    KNOWLEDGE_SOURCE_READ_PERMISSIONS
                ),
            )
            .limit(1)
        )
        if user_perm.scalar_one_or_none():
            return True
        # Project access is owned by ProjectRepository/project_permissions.
        # In particular, a ProjectMember row alone is not a grant: the project
        # must be live and the member must have an effective ``read`` ACL.
        from ..memory.project_repository import ProjectRepository

        for permission in source.permissions or []:
            if (
                permission.project_id is None
                or str(permission.permission or "").strip().lower()
                not in KNOWLEDGE_SOURCE_READ_PERMISSIONS
            ):
                continue
            if await ProjectRepository.has_permission(
                session,
                project_id=permission.project_id,
                user_id=actor_user_id,
                permission="read",
            ):
                return True
        return False

    @staticmethod
    async def can_write_source(
        session: AsyncSession,
        *,
        source_id: uuid.UUID,
        actor_user_id: Optional[uuid.UUID],
        is_admin: bool = False,
    ) -> bool:
        if is_admin:
            return True
        if actor_user_id is None:
            return False
        source = await KnowledgeService.get_source(session, source_id)
        if not source:
            return False
        if source.owner_user_id == actor_user_id:
            return True
        result = await session.execute(
            select(KnowledgeSourcePermission.id)
            .where(
                KnowledgeSourcePermission.source_id == source_id,
                KnowledgeSourcePermission.user_id == actor_user_id,
                KnowledgeSourcePermission.permission.in_(("write", "owner")),
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    @staticmethod
    async def list_sources(
        session: AsyncSession,
        *,
        actor_user_id: Optional[uuid.UUID],
        is_admin: bool = False,
    ) -> list[KnowledgeSource]:
        result = await session.execute(
            select(KnowledgeSource).order_by(KnowledgeSource.created_at.desc())
        )
        sources = list(result.scalars().all())
        if is_admin:
            return sources
        visible: list[KnowledgeSource] = []
        for source in sources:
            if await KnowledgeService.can_read_source(
                session,
                source_id=source.id,
                actor_user_id=actor_user_id,
                is_admin=False,
            ):
                visible.append(source)
        return visible

    @staticmethod
    async def delete_source(
        session: AsyncSession,
        *,
        source_id: uuid.UUID,
        actor_user_id: Optional[uuid.UUID],
        is_admin: bool = False,
    ) -> bool:
        if not await KnowledgeService.can_write_source(
            session,
            source_id=source_id,
            actor_user_id=actor_user_id,
            is_admin=is_admin,
        ):
            return False
        source = await KnowledgeService.get_source(session, source_id)
        if not source:
            return False
        await session.delete(source)
        return True

    @staticmethod
    def _matches_patterns(path: Path, root: Path, include: list[str], exclude: list[str]) -> bool:
        rel = path.relative_to(root).as_posix()
        name = path.name
        parts = set(path.relative_to(root).parts)
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            return False
        included = any(fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern) for pattern in include)
        if not included:
            return False
        for pattern in exclude:
            normalized = pattern.strip().rstrip("/")
            if not normalized:
                continue
            if normalized in parts:
                return False
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
                return False
        return True

    @staticmethod
    def _read_file_text(path: Path, max_chars: int = 120_000) -> tuple[str, Optional[str]]:
        try:
            from ..services.project_information_organizer import _extract_file_text

            return _extract_file_text(path, max_chars)
        except Exception as exc:
            if path.suffix.lower() in TEXT_EXTENSIONS:
                try:
                    return path.read_text(encoding="utf-8", errors="replace")[:max_chars], None
                except Exception as read_exc:  # pragma: no cover - filesystem dependent
                    return "", str(read_exc)
            return "", str(exc)

    @staticmethod
    def _content_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()

    @staticmethod
    def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
        if not text.startswith("---"):
            return {}, text
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            return {}, text
        for index in range(1, min(len(lines), 400)):
            if lines[index].strip() == "---":
                raw = "\n".join(lines[1:index])
                body = "\n".join(lines[index + 1 :])
                try:
                    parsed = yaml.safe_load(raw) or {}
                    if isinstance(parsed, dict):
                        return KnowledgeService._json_safe_metadata(parsed), body
                except Exception:
                    return {}, body
        return {}, text

    @staticmethod
    def _json_safe_metadata(value: Any) -> Any:
        """Keep YAML metadata safe for the JSON-backed document column."""

        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, dict):
            return {
                str(key): KnowledgeService._json_safe_metadata(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [KnowledgeService._json_safe_metadata(item) for item in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    @staticmethod
    def _extract_title(path: Path, frontmatter: dict[str, Any], body: str) -> str:
        title = frontmatter.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
        for line in body.splitlines():
            match = _HEADING_RE.match(line)
            if match:
                return match.group(2).strip()
        return path.stem

    @staticmethod
    def _normalize_tags(frontmatter: dict[str, Any]) -> list[str]:
        raw = frontmatter.get("tags") or frontmatter.get("tag") or []
        if isinstance(raw, str):
            raw = [item.strip() for item in raw.replace(",", " ").split()]
        if not isinstance(raw, list):
            return []
        return [str(item).strip().lstrip("#") for item in raw if str(item).strip()]

    @staticmethod
    def _normalize_refs(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        return [str(value)]

    _parse_document_datetime = staticmethod(document_dates.parse_document_datetime)
    _frontmatter_document_date = staticmethod(document_dates.frontmatter_document_date)
    _filename_document_date = staticmethod(document_dates.filename_document_date)
    _growi_document_date = staticmethod(document_dates.growi_document_date)
    _derive_document_date = staticmethod(document_dates.derive_document_date)

    @staticmethod
    def _parse_query_date_bound(
        value: date | datetime | str,
        *,
        end: bool,
    ) -> tuple[datetime, bool]:
        """Return a UTC date bound and whether it is an exclusive day end."""

        parsed = KnowledgeService._parse_document_datetime(value)
        if parsed is None:
            raise ValueError("date_from/date_to は ISO 8601 日時または日付で指定してください")
        is_date_only = isinstance(value, date) and not isinstance(value, datetime)
        if isinstance(value, str):
            is_date_only = bool(
                re.fullmatch(
                    r"(?:\d{4}-\d{2}-\d{2}|\d{4}/\d{2}/\d{2}|\d{8})",
                    value.strip(),
                )
            )
        if end and is_date_only:
            return parsed + timedelta(days=1), True
        return parsed, False

    @staticmethod
    def _chunk_text(text: str, *, max_chars: int = 2400) -> list[dict[str, Any]]:
        if not text.strip():
            return []
        chunks: list[dict[str, Any]] = []
        heading_path: list[str] = []
        buffer: list[str] = []

        def flush() -> None:
            if not buffer:
                return
            joined = "\n".join(buffer).strip()
            buffer.clear()
            while len(joined) > max_chars:
                part = joined[:max_chars].strip()
                chunks.append({"text": part, "heading_path": list(heading_path)})
                joined = joined[max_chars:].strip()
            if joined:
                chunks.append({"text": joined, "heading_path": list(heading_path)})

        for line in text.splitlines():
            match = _HEADING_RE.match(line)
            if match:
                flush()
                level = len(match.group(1))
                heading_path = heading_path[: level - 1] + [match.group(2).strip()]
            buffer.append(line)
            if sum(len(item) + 1 for item in buffer) >= max_chars:
                flush()
        flush()
        return chunks

    @staticmethod
    def _extract_links(text: str) -> list[dict[str, str]]:
        links: list[dict[str, str]] = []
        for target in _WIKI_LINK_RE.findall(text):
            links.append({"target_path_or_url": target.strip(), "link_type": "wiki"})
        for target in _MARKDOWN_LINK_RE.findall(text):
            if target.strip():
                link_type = "url" if target.startswith(("http://", "https://")) else "markdown"
                links.append({"target_path_or_url": target.strip(), "link_type": link_type})
        return links

    @staticmethod
    async def sync_source(
        session: AsyncSession,
        *,
        source_id: uuid.UUID,
        actor_user_id: Optional[uuid.UUID],
        is_admin: bool = False,
        max_files: int = 20_000,
    ) -> dict[str, Any]:
        if not await KnowledgeService.can_write_source(
            session,
            source_id=source_id,
            actor_user_id=actor_user_id,
            is_admin=is_admin,
        ):
            raise PermissionError("ナレッジソースの同期権限がありません")
        source = await KnowledgeService.get_source(session, source_id)
        if not source:
            raise ValueError("ナレッジソースが見つかりません")

        source.status = "syncing"
        source.error_message = None
        await session.flush()

        if source.source_type == "growi":
            indexed, changed, errors = await KnowledgeService._sync_growi_source(
                session, source, max_files=max_files
            )
        else:
            indexed, changed, errors = await KnowledgeService._sync_local_source(
                session, source, max_files=max_files
            )

        counts = await KnowledgeService.recount_source(session, source.id)
        source.status = "error" if errors else "synced"
        source.last_synced_at = datetime.utcnow()
        source.error_message = "\n".join(errors[:20]) if errors else None
        index_payload: dict[str, Any] | None = None
        if not errors:
            try:
                from .index_service import get_knowledge_index_service

                index_result = await get_knowledge_index_service().sync_source(
                    session,
                    source.id,
                )
                index_payload = index_result.to_dict()
            except Exception as exc:
                logger.exception("Knowledge index sync failed for source %s", source.id)
                index_payload = {"status": "error", "indexed_chunks": 0, "error": str(exc)}
        if index_payload and index_payload.get("status") in {"error", "unavailable"}:
            source.status = "error"
            source.error_message = "Knowledge derived index synchronization failed; retry source sync"
            errors.append(source.error_message)
        return {
            "source": source.to_dict(),
            "indexed_files": indexed,
            "changed_documents": changed,
            "deleted_documents": counts["deleted_documents"],
            "errors": errors,
            "index": index_payload,
        }

    @staticmethod
    async def _sync_local_source(
        session: AsyncSession,
        source: KnowledgeSource,
        *,
        max_files: int,
    ) -> tuple[int, int, list[str]]:
        """ローカルディレクトリソースをファイル走査で同期する。"""
        root = lexical_path(source.root_path)

        existing_result = await session.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.source_id == source.id)
        )
        existing = {doc.path: doc for doc in existing_result.scalars().all()}
        seen_paths: set[str] = set()
        indexed = 0
        changed = 0
        errors: list[str] = []

        include = KnowledgeService.normalize_patterns(
            source.include_patterns, DEFAULT_INCLUDE_PATTERNS
        )
        exclude = KnowledgeService.normalize_patterns(
            source.exclude_patterns, DEFAULT_EXCLUDE_PATTERNS
        )

        try:
            scan = await scan_source(root, include, exclude, max_files, KnowledgeService._matches_patterns)
        except (StorageError, OSError) as exc:
            return 0, 0, [f"Knowledge Source storage unavailable; existing documents preserved: {exc}"]
        complete = scan.complete
        errors.extend(scan.errors)
        # An unregistered legacy mount can leave an empty local mountpoint.
        # Never interpret that ambiguous state as permission to delete all rows.
        if not scan.paths and any(doc.status != "deleted" for doc in existing.values()):
            return 0, 0, ["Knowledge Source is empty or unavailable; existing documents preserved"]
        for path in scan.paths:
            rel_path = path.relative_to(root).as_posix()
            seen_paths.add(rel_path)
            indexed += 1
            try:
                text, extract_error, stat = await read_source_file(
                    root, path, KnowledgeService._read_file_text,
                )
            except (StorageError, OSError) as exc:
                complete = False
                errors.append(f"{rel_path}: storage unavailable; previous document preserved: {exc}")
                continue
            if extract_error:
                complete = False
                errors.append(f"{rel_path}: {extract_error}; previous document preserved")
                continue
            digest = KnowledgeService._content_hash(text)
            frontmatter, body = KnowledgeService._parse_frontmatter(text)
            tags = KnowledgeService._normalize_tags(frontmatter)
            project_refs = KnowledgeService._normalize_refs(
                frontmatter.get("project") or frontmatter.get("projects")
            )
            managed_project_id = KnowledgeService._source_project_id(source)
            if managed_project_id and managed_project_id not in project_refs:
                project_refs.append(managed_project_id)
            task_refs = KnowledgeService._normalize_refs(
                frontmatter.get("task") or frontmatter.get("tasks")
            )

            document = existing.get(rel_path)
            is_changed = document is None or document.content_hash != digest
            if document is None:
                document = KnowledgeDocument(source_id=source.id, path=rel_path)
                session.add(document)
            document.resolved_absolute_path = str(path)
            document.title = KnowledgeService._extract_title(path, frontmatter, body)
            document.extension = path.suffix.lower()
            document.mime_type = mimetypes.guess_type(path.name)[0]
            document.content_hash = digest
            modified_at_utc = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            document.modified_at = modified_at_utc.replace(tzinfo=None)
            (
                document.document_date,
                document.document_date_source,
            ) = KnowledgeService._derive_document_date(
                frontmatter,
                rel_path,
                modified_at=modified_at_utc,
            )
            document.size_bytes = stat.st_size
            document.frontmatter_json = frontmatter
            document.tags = tags
            document.project_refs = project_refs
            document.task_refs = task_refs
            document.status = "error" if extract_error else "active"
            document.error_message = extract_error
            document.last_indexed_at = datetime.utcnow()
            document.updated_at = datetime.utcnow()
            await session.flush()

            if is_changed:
                changed += 1
                await session.execute(
                    delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)
                )
                await session.execute(
                    delete(KnowledgeLink).where(
                        KnowledgeLink.source_document_id == document.id
                    )
                )
                for index, chunk in enumerate(KnowledgeService._chunk_text(body or text)):
                    chunk_text = chunk["text"]
                    session.add(
                        KnowledgeChunk(
                            document_id=document.id,
                            heading_path=chunk["heading_path"],
                            chunk_index=index,
                            text=chunk_text,
                            token_count=max(1, len(chunk_text) // 4),
                            content_hash=KnowledgeService._content_hash(chunk_text),
                            metadata_json={
                                "source_id": str(source.id),
                                "document_id": str(document.id),
                                "path": rel_path,
                                "tags": tags,
                                "project_refs": project_refs,
                            },
                        )
                    )
                for link in KnowledgeService._extract_links(body or text):
                    session.add(
                        KnowledgeLink(
                            source_document_id=document.id,
                            target_path_or_url=link["target_path_or_url"],
                            link_type=link["link_type"],
                        )
                    )
            if extract_error:
                errors.append(f"{rel_path}: {extract_error}")

        try:
            await verify_source_online(root)
        except (StorageError, OSError):
            complete = False
            errors.append("Knowledge Source disconnected; unvisited documents preserved")
        for rel_path, document in existing.items():
            if complete and not errors and rel_path not in seen_paths and document.status != "deleted":
                document.status = "deleted"
                document.updated_at = datetime.utcnow()

        await session.flush()
        return indexed, changed, errors

    @staticmethod
    async def _sync_growi_source(
        session: AsyncSession,
        source: KnowledgeSource,
        *,
        max_files: int,
    ) -> tuple[int, int, list[str]]:
        """GROWI 社内Wiki を REST API 経由で差分同期する。

        ページのリビジョンID（無ければ更新日時）を変更キーとして保持し、
        前回同期から変わっていないページは本文を再取得せずスキップする。
        """
        from .growi_client import GrowiClient, GrowiClientError

        token = source.growi_api_token
        if not token:
            return 0, 0, ["GROWI ソースに API トークンが設定されていません"]

        access_policy = source.access_policy or {}
        overrides = access_policy.get("growi_endpoints") or {}
        list_root = access_policy.get("growi_root_path") or "/"
        client = GrowiClient(
            base_url=source.root_path,
            api_token=token,
            endpoint_overrides=overrides if isinstance(overrides, dict) else {},
        )

        include = KnowledgeService.normalize_patterns(source.include_patterns, ["*"])
        exclude = KnowledgeService.normalize_patterns(
            source.exclude_patterns, DEFAULT_GROWI_EXCLUDE_PATTERNS
        )

        existing_result = await session.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.source_id == source.id)
        )
        existing = {doc.path: doc for doc in existing_result.scalars().all()}
        seen_paths: set[str] = set()
        indexed = 0
        changed = 0
        errors: list[str] = []
        managed_project_id = KnowledgeService._source_project_id(source)

        try:
            pages = await client.list_pages(list_root)
        except GrowiClientError as exc:
            logger.exception("GROWI ページ列挙に失敗 source=%s", source.id)
            return 0, 0, [str(exc)]

        complete = True
        for page in sorted(pages, key=lambda item: item.path.lower()):
            if not KnowledgeService._growi_path_matches(page.path, include, exclude):
                continue
            if indexed >= max_files:
                complete = False
                errors.append("Source scan incomplete: max_files reached; unvisited documents preserved")
                break

            rel_path = page.path
            seen_paths.add(rel_path)
            indexed += 1

            document = existing.get(rel_path)
            prev_meta = (document.frontmatter_json or {}).get("growi") if document else None
            prev_key = prev_meta.get("change_key") if isinstance(prev_meta, dict) else None
            need_fetch = (
                document is None
                or not document.content_hash
                or not prev_key
                or prev_key != page.change_key
            )

            if document is not None and not need_fetch:
                # 変更なし: 本文再取得もチャンク再構築も行わない。
                metadata = dict(document.frontmatter_json or {})
                growi_metadata = dict(metadata.get("growi") or {})
                if KnowledgeService._parse_document_datetime(page.updated_at) is not None:
                    growi_metadata["updated_at"] = page.updated_at
                    document.modified_at = KnowledgeService._parse_iso_datetime(page.updated_at)
                growi_metadata.update(page_id=page.page_id, change_key=page.change_key)
                if page.revision_id:
                    growi_metadata["revision_id"] = page.revision_id
                metadata["growi"] = growi_metadata
                document.frontmatter_json = metadata
                if getattr(document, "document_date_source", None) == "unknown":
                    # The repair migration explicitly rejected historical
                    # naive mtime. An unchanged page without fresh date
                    # evidence must not resurrect that rejected timestamp.
                    repaired = document_dates.repair_unknown_legacy_date(
                        metadata, rel_path, growi_updated_at=page.updated_at,
                    )
                    document.document_date = repaired.document_date
                    document.document_date_source = repaired.document_date_source
                    continue
                (
                    document.document_date,
                    document.document_date_source,
                ) = KnowledgeService._derive_document_date(
                    (document.frontmatter_json or {})
                    if isinstance(document.frontmatter_json, dict)
                    else {},
                    rel_path,
                    growi_updated_at=page.updated_at,
                    modified_at=(
                        document.document_date
                        if getattr(document, "document_date_source", None) == "modified_at"
                        else None
                    ),
                )
                continue

            try:
                body = await client.get_page_body(page)
            except GrowiClientError as exc:
                errors.append(f"{rel_path}: {exc}")
                if document is None:
                    document = KnowledgeDocument(source_id=source.id, path=rel_path)
                    session.add(document)
                document.status = "error"
                document.error_message = str(exc)
                document.updated_at = datetime.utcnow()
                await session.flush()
                continue

            digest = KnowledgeService._content_hash(body)
            frontmatter, parsed_body = KnowledgeService._parse_frontmatter(body)
            tags = KnowledgeService._normalize_tags(frontmatter)
            project_refs = KnowledgeService._normalize_refs(
                frontmatter.get("project") or frontmatter.get("projects")
            )
            if managed_project_id and managed_project_id not in project_refs:
                project_refs.append(managed_project_id)
            task_refs = KnowledgeService._normalize_refs(
                frontmatter.get("task") or frontmatter.get("tasks")
            )

            is_new = document is None
            is_changed = is_new or document.content_hash != digest
            if document is None:
                document = KnowledgeDocument(source_id=source.id, path=rel_path)
                session.add(document)

            fm_json = dict(frontmatter) if isinstance(frontmatter, dict) else {}
            fm_json["growi"] = {
                "page_id": page.page_id,
                "revision_id": page.revision_id,
                "updated_at": page.updated_at,
                "change_key": page.change_key,
            }
            document.resolved_absolute_path = None
            document.title = KnowledgeService._extract_title(
                Path(rel_path.rsplit("/", 1)[-1] or rel_path), frontmatter, parsed_body
            )
            document.extension = ".md"
            document.mime_type = "text/markdown"
            document.content_hash = digest
            modified_at = KnowledgeService._parse_iso_datetime(page.updated_at)
            document.modified_at = modified_at
            (
                document.document_date,
                document.document_date_source,
            ) = KnowledgeService._derive_document_date(
                frontmatter,
                rel_path,
                growi_updated_at=page.updated_at,
                modified_at=modified_at,
            )
            document.size_bytes = len(body.encode("utf-8", errors="replace"))
            document.frontmatter_json = fm_json
            document.tags = tags
            document.project_refs = project_refs
            document.task_refs = task_refs
            document.status = "active"
            document.error_message = None
            document.last_indexed_at = datetime.utcnow()
            document.updated_at = datetime.utcnow()
            await session.flush()

            if is_changed:
                changed += 1
                await session.execute(
                    delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)
                )
                await session.execute(
                    delete(KnowledgeLink).where(
                        KnowledgeLink.source_document_id == document.id
                    )
                )
                page_url = build_page_url(source.root_path, rel_path)
                for index, chunk in enumerate(
                    KnowledgeService._chunk_text(parsed_body or body)
                ):
                    chunk_text = chunk["text"]
                    session.add(
                        KnowledgeChunk(
                            document_id=document.id,
                            heading_path=chunk["heading_path"],
                            chunk_index=index,
                            text=chunk_text,
                            token_count=max(1, len(chunk_text) // 4),
                            content_hash=KnowledgeService._content_hash(chunk_text),
                            metadata_json={
                                "source_id": str(source.id),
                                "document_id": str(document.id),
                                "path": rel_path,
                                "url": page_url,
                                "tags": tags,
                                "project_refs": project_refs,
                            },
                        )
                    )
                for link in KnowledgeService._extract_links(parsed_body or body):
                    session.add(
                        KnowledgeLink(
                            source_document_id=document.id,
                            target_path_or_url=link["target_path_or_url"],
                            link_type=link["link_type"],
                        )
                    )

        for rel_path, document in existing.items():
            if complete and rel_path not in seen_paths and document.status != "deleted":
                document.status = "deleted"
                document.updated_at = datetime.utcnow()

        await session.flush()
        return indexed, changed, errors

    @staticmethod
    def _growi_path_matches(path: str, include: list[str], exclude: list[str]) -> bool:
        normalized = path if path.startswith("/") else f"/{path}"
        included = (
            any(fnmatch.fnmatch(normalized, pattern) for pattern in include)
            if include
            else True
        )
        if not included:
            return False
        for pattern in exclude:
            pattern = pattern.strip()
            if pattern and fnmatch.fnmatch(normalized, pattern):
                return False
        return True

    @staticmethod
    def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
        parsed = KnowledgeService._parse_document_datetime(value)
        return parsed.replace(tzinfo=None) if parsed is not None else None

    @staticmethod
    def _source_project_id(source: KnowledgeSource) -> Optional[str]:
        policy = source.access_policy or {}
        project_id = policy.get("project_id")
        return str(project_id) if project_id else None

    @staticmethod
    async def recount_source(session: AsyncSession, source_id: uuid.UUID) -> dict[str, int]:
        doc_count = await session.scalar(
            select(func.count(KnowledgeDocument.id)).where(
                KnowledgeDocument.source_id == source_id,
                KnowledgeDocument.status == "active",
            )
        )
        deleted_count = await session.scalar(
            select(func.count(KnowledgeDocument.id)).where(
                KnowledgeDocument.source_id == source_id,
                KnowledgeDocument.status == "deleted",
            )
        )
        chunk_count = await session.scalar(
            select(func.count(KnowledgeChunk.id))
            .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
            .where(
                KnowledgeDocument.source_id == source_id,
                KnowledgeDocument.status == "active",
            )
        )
        source = await KnowledgeService.get_source(session, source_id)
        if source:
            source.document_count = int(doc_count or 0)
            source.chunk_count = int(chunk_count or 0)
        return {
            "documents": int(doc_count or 0),
            "chunks": int(chunk_count or 0),
            "deleted_documents": int(deleted_count or 0),
        }

    @staticmethod
    async def _readable_source_ids(
        session: AsyncSession,
        *,
        source_id: Optional[uuid.UUID | str],
        actor_user_id: Optional[uuid.UUID],
        is_admin: bool,
    ) -> list[uuid.UUID]:
        """Batch source grants, using the canonical project ACL evaluator.

        Only authorization metadata is materialized; document queries share
        this result and never perform per-document or per-project ACL queries.
        """
        from ..memory.models import Project, ProjectMember, User
        from ..services.project_permissions import has_effective_project_permission

        if actor_user_id is None and not is_admin:
            return []
        normalized_source_id = KnowledgeService._coerce_uuid(source_id)
        source_stmt = select(KnowledgeSource.id)
        if normalized_source_id is not None:
            source_stmt = source_stmt.where(KnowledgeSource.id == normalized_source_id)
        if is_admin:
            return list((await session.execute(source_stmt)).scalars().all())
        stmt = (
            source_stmt.add_columns(
                KnowledgeSource.owner_user_id,
                KnowledgeSourcePermission.user_id,
                KnowledgeSourcePermission.permission,
                Project.id.label("project_id"),
                Project.owner_id.label("project_owner_id"),
                User.id.label("actor_id"),
                User.role.label("user_role"),
                ProjectMember.permissions.label("member_permissions"),
            )
            .outerjoin(KnowledgeSourcePermission,
                       KnowledgeSourcePermission.source_id == KnowledgeSource.id)
            .outerjoin(Project, and_(
                Project.id == KnowledgeSourcePermission.project_id,
                Project.deleted_at.is_(None),
            ))
            .outerjoin(User, User.id == actor_user_id)
            .outerjoin(ProjectMember, and_(
                ProjectMember.project_id == Project.id,
                ProjectMember.user_id == actor_user_id,
            ))
        )
        readable: set[uuid.UUID] = set()
        for row in (await session.execute(stmt)).all():
            if row.id in readable:
                continue
            direct = (row.user_id == actor_user_id
                      and row.permission in KNOWLEDGE_SOURCE_READ_PERMISSIONS)
            project = (
                row.project_id is not None and row.actor_id is not None
                and str(row.permission or "").strip().lower()
                in KNOWLEDGE_SOURCE_READ_PERMISSIONS
                and has_effective_project_permission(
                    user_id=actor_user_id, user_role=row.user_role,
                    project_owner_id=row.project_owner_id,
                    member_permissions=row.member_permissions, permission="read",
                )
            )
            if row.owner_user_id == actor_user_id or direct or project:
                readable.add(row.id)
        return sorted(readable, key=str)

    @staticmethod
    def _normalize_query_filters(
        filters: Optional[KnowledgeQueryFilters],
    ) -> tuple[KnowledgeQueryFilters, Optional[datetime], Optional[datetime], bool]:
        filters = filters or KnowledgeQueryFilters()
        source_id = KnowledgeService._coerce_uuid(filters.source_id)
        project_id = KnowledgeService._coerce_uuid(filters.project_id)
        raw_tags = filters.tags
        if isinstance(raw_tags, str):
            raw_tags = (raw_tags,)
        tags = tuple(
            str(tag).strip().lower()
            for tag in (raw_tags or ())
            if str(tag).strip()
        )
        extension = str(filters.extension or "").strip().lower()
        if extension and not extension.startswith("."):
            extension = f".{extension}"
        path_prefix = str(filters.path_prefix or "").strip().replace("\\", "/")
        status = str(filters.status or "active").strip().lower()
        if status not in KNOWLEDGE_DOCUMENT_STATUSES and status != "all":
            raise ValueError("status is not an allowed Knowledge document status")

        date_from = None
        date_to = None
        date_to_exclusive = False
        if filters.date_from is not None:
            date_from, _ = KnowledgeService._parse_query_date_bound(
                filters.date_from,
                end=False,
            )
        if filters.date_to is not None:
            date_to, date_to_exclusive = KnowledgeService._parse_query_date_bound(
                filters.date_to,
                end=True,
            )
        if date_from is not None and date_to is not None and date_from > date_to:
            raise ValueError("date_from must not be later than date_to")
        normalized = KnowledgeQueryFilters(
            source_id=source_id,
            project_id=project_id,
            tags=tags,
            extension=extension or None,
            path_prefix=path_prefix or None,
            status=status,
            date_from=filters.date_from,
            date_to=filters.date_to,
        )
        return normalized, date_from, date_to, date_to_exclusive

    @staticmethod
    def _query_json_values(column: Any, dialect: str) -> Any:
        """Expand only JSON arrays; legacy null/scalar/object values are empty."""
        if dialect == "postgresql":
            array = case(
                (func.json_typeof(column) == "array", column),
                else_=cast([], JSON),
            )
            return func.json_array_elements_text(array).table_valued("value")
        if dialect == "sqlite":
            array = case((func.json_type(column) == "array", column), else_="[]")
            return func.json_each(array).table_valued("value")
        raise ValueError("Structured Knowledge queries require PostgreSQL or SQLite")

    @staticmethod
    def _query_trim(value: Any, dialect: str) -> Any:
        trim = func.btrim if dialect == "postgresql" else func.trim
        return trim(cast(value, String), " \t\n\r\v\f")

    @staticmethod
    async def _structured_query_relation(
        session: AsyncSession,
        *,
        filters: KnowledgeQueryFilters,
        actor_user_id: Optional[uuid.UUID],
        is_admin: bool,
        date_from: Optional[datetime],
        date_to: Optional[datetime],
        date_to_exclusive: bool,
    ) -> Any:
        readable_source_ids = await KnowledgeService._readable_source_ids(
            session,
            source_id=filters.source_id,
            actor_user_id=actor_user_id,
            is_admin=is_admin,
        )
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            # One array parameter avoids asyncpg's bind-count ceiling when a
            # user can read many sources. ACL rows never multiply documents.
            from sqlalchemy.dialects.postgresql import ARRAY, UUID
            source_match = KnowledgeDocument.source_id == any_(
                cast(readable_source_ids, ARRAY(UUID(as_uuid=True)))
            )
        else:
            source_match = KnowledgeDocument.source_id.in_(readable_source_ids)
        conditions = [source_match]
        if filters.status != "all":
            conditions.append(KnowledgeDocument.status == filters.status)
        if filters.extension:
            conditions.append(KnowledgeDocument.extension == filters.extension)
        if filters.path_prefix:
            conditions.append(
                KnowledgeDocument.path.startswith(filters.path_prefix, autoescape=True)
            )
        if date_from is not None:
            conditions.append(KnowledgeDocument.document_date >= date_from)
        if date_to is not None:
            if date_to_exclusive:
                conditions.append(KnowledgeDocument.document_date < date_to)
            else:
                conditions.append(KnowledgeDocument.document_date <= date_to)

        if filters.tags:
            values = KnowledgeService._query_json_values(KnowledgeDocument.tags, dialect)
            tag = func.lower(KnowledgeService._query_trim(values.c.value, dialect))
            for wanted in set(filters.tags):
                conditions.append(select(1).select_from(values).where(tag == wanted).exists())
        if filters.project_id:
            values = KnowledgeService._query_json_values(
                KnowledgeDocument.project_refs, dialect,
            )
            conditions.append(select(1).select_from(values).where(
                values.c.value == str(filters.project_id)
            ).exists())
        return (
            select(
                KnowledgeDocument.id.label("document_id"), KnowledgeDocument.source_id,
                KnowledgeSource.name.label("source_name"), KnowledgeDocument.extension,
                KnowledgeDocument.tags, KnowledgeDocument.project_refs,
                KnowledgeDocument.status, KnowledgeDocument.document_date_source,
            )
            .join(KnowledgeSource, KnowledgeDocument.source_id == KnowledgeSource.id)
            .where(and_(*conditions))
            .cte("knowledge_matches")
        )

    @staticmethod
    def _structured_groups(matches: Any, group_by: str, dialect: str) -> Any:
        if group_by in {"tag", "project_id"}:
            column = matches.c.tags if group_by == "tag" else matches.c.project_refs
            values = KnowledgeService._query_json_values(column, dialect)
            trimmed = KnowledgeService._query_trim(values.c.value, dialect)
            key = trimmed if group_by == "tag" else values.c.value
            # Outer expansion gives empty arrays their null bucket; DISTINCT
            # counts each document once even when a JSON array repeats a value.
            members = select(matches.c.document_id, key.label("key")).select_from(
                matches.outerjoin(values, trimmed != "")
            ).distinct().subquery()
            return select(members.c.key, func.count().label("count")).group_by(
                members.c.key
            ).subquery("knowledge_groups")
        fields = {
            "source_id": matches.c.source_id,
            "source": func.nullif(matches.c.source_name, ""),
            "extension": func.nullif(matches.c.extension, ""),
            "status": func.nullif(matches.c.status, ""),
            "date_source": case(
                (KnowledgeService._query_trim(matches.c.document_date_source, dialect) == "", None),
                else_=matches.c.document_date_source,
            ),
        }
        key = fields[group_by]
        return select(key.label("key"), func.count().label("count")).select_from(
            matches
        ).group_by(key).subquery("knowledge_groups")

    @staticmethod
    async def structured_query(
        session: AsyncSession,
        *,
        actor_user_id: Optional[uuid.UUID],
        is_admin: bool = False,
        operation: str = "list",
        filters: Optional[KnowledgeQueryFilters] = None,
        source_id: Optional[uuid.UUID | str] = None,
        project_id: Optional[uuid.UUID | str] = None,
        tags: Optional[Iterable[str]] = None,
        extension: Optional[str] = None,
        path_prefix: Optional[str] = None,
        status: Optional[str] = None,
        date_from: Optional[date | datetime | str] = None,
        date_to: Optional[date | datetime | str] = None,
        limit: int = 20,
        offset: int = 0,
        order_by: str = "date",
        order: str = "desc",
        group_by: Optional[str] = None,
    ) -> dict[str, Any]:
        """Run an ACL-safe exact Knowledge count, list, or metadata group.

        Every model-facing choice is validated against finite allowlists and
        translated into SQLAlchemy expressions.  There is intentionally no
        raw SQL or caller-provided column/expression surface here.

        ``offset`` counts documents for list and buckets for group. Both are
        bounded by ``limit`` and return ``next_offset`` (None at the end).
        ``total_matches`` always counts documents; ``group_total`` counts all
        buckets before pagination. Groups sort by count descending then key;
        lists sort by date in the requested direction, then path and ID.
        """

        operation = str(operation or "list").strip().lower()
        if operation not in KNOWLEDGE_QUERY_OPERATIONS:
            raise ValueError("operation must be one of: count, list, group")
        order_by = str(order_by or "date").strip().lower()
        if order_by not in KNOWLEDGE_QUERY_ORDER_FIELDS:
            raise ValueError("order_by must be date")
        order = str(order or "desc").strip().lower()
        if order not in KNOWLEDGE_QUERY_ORDER_DIRECTIONS:
            raise ValueError("order must be asc or desc")
        if group_by is not None:
            group_by = str(group_by).strip().lower()
            if group_by not in KNOWLEDGE_QUERY_GROUP_FIELDS:
                raise ValueError("group_by is not an allowed Knowledge metadata field")
        if operation == "group" and not group_by:
            raise ValueError("group_by is required for a group operation")
        if operation != "group" and group_by:
            raise ValueError("group_by is only valid for a group operation")

        try:
            limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("limit must be an integer") from exc
        if limit < 1:
            raise ValueError("limit must be at least 1")
        limit = min(limit, KNOWLEDGE_QUERY_MAX_LIMIT)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")

        direct_filter_values = (
            source_id,
            project_id,
            tags,
            extension,
            path_prefix,
            status,
            date_from,
            date_to,
        )
        if filters is not None and any(value is not None for value in direct_filter_values):
            raise ValueError("filters cannot be combined with direct Knowledge filters")
        if filters is None:
            direct_tags = (
                (tags,)
                if isinstance(tags, str)
                else tuple(tags or ())
            )
            filters = KnowledgeQueryFilters(
                source_id=source_id,
                project_id=project_id,
                tags=direct_tags,
                extension=extension,
                path_prefix=path_prefix,
                status=status or "active",
                date_from=date_from,
                date_to=date_to,
            )
        normalized_filters, date_from, date_to, date_to_exclusive = (
            KnowledgeService._normalize_query_filters(filters)
        )
        matches = await KnowledgeService._structured_query_relation(
            session,
            filters=normalized_filters,
            actor_user_id=actor_user_id,
            is_admin=is_admin,
            date_from=date_from,
            date_to=date_to,
            date_to_exclusive=date_to_exclusive,
        )
        total_count = int((await session.execute(
            select(func.count()).select_from(matches)
        )).scalar_one())

        if operation == "count":
            return {
                "operation": "count",
                "count": total_count,
                "total_matches": total_count,
            }

        if operation == "group":
            grouped = KnowledgeService._structured_groups(
                matches, group_by or "", session.get_bind().dialect.name,
            )
            group_total = int((await session.execute(
                select(func.count()).select_from(grouped)
            )).scalar_one())
            result = await session.execute(
                select(grouped).order_by(
                    grouped.c.count.desc(), grouped.c.key.asc().nullsfirst(),
                ).limit(limit).offset(offset)
            )
            groups = [
                {"key": str(row.key) if row.key is not None else None, "count": int(row.count)}
                for row in result.all()
            ]
            has_more = offset + len(groups) < group_total
            return {
                "operation": "group",
                "group_by": group_by,
                "count": total_count,
                "total_matches": total_count,
                "group_total": group_total,
                "returned": len(groups),
                "returned_count": len(groups),
                "limit": limit,
                "offset": offset,
                "has_more": has_more,
                "next_offset": offset + len(groups) if has_more else None,
                "truncated": group_total > len(groups),
                "groups": groups,
            }

        date_order = (
            KnowledgeDocument.document_date.asc()
            if order == "asc" else KnowledgeDocument.document_date.desc()
        )
        result = await session.execute(
            select(KnowledgeDocument, KnowledgeSource)
            .join(KnowledgeSource, KnowledgeDocument.source_id == KnowledgeSource.id)
            .join(matches, matches.c.document_id == KnowledgeDocument.id)
            .order_by(date_order.nullslast(), KnowledgeDocument.path.asc(), KnowledgeDocument.id.asc())
            .limit(limit).offset(offset)
        )
        documents = []
        for document, source in result.all():
            payload = document.to_dict()
            payload["source"] = source.to_dict()
            documents.append(payload)
        return {
            "operation": "list",
            "count": total_count,
            "total_count": total_count,
            "total_matches": total_count,
            "returned_count": len(documents),
            "returned": len(documents),
            "truncated": total_count > len(documents),
            "has_more": offset + len(documents) < total_count,
            "next_offset": (
                offset + len(documents) if offset + len(documents) < total_count else None
            ),
            "limit": limit,
            "offset": offset,
            "order_by": order_by,
            "order": order,
            "items": documents,
            "documents": documents,
        }

    @staticmethod
    async def query(
        session: AsyncSession,
        *,
        actor_user_id: Optional[uuid.UUID],
        is_admin: bool = False,
        operation: str = "list",
        filters: Optional[KnowledgeQueryFilters] = None,
        source_id: Optional[uuid.UUID | str] = None,
        project_id: Optional[uuid.UUID | str] = None,
        tags: Optional[Iterable[str]] = None,
        extension: Optional[str] = None,
        path_prefix: Optional[str] = None,
        status: Optional[str] = None,
        date_from: Optional[date | datetime | str] = None,
        date_to: Optional[date | datetime | str] = None,
        limit: int = 20,
        offset: int = 0,
        order_by: str = "date",
        order: str = "desc",
        group_by: Optional[str] = None,
    ) -> dict[str, Any]:
        """Compatibility alias for callers that name the operation ``query``."""

        return await KnowledgeService.structured_query(
            session,
            actor_user_id=actor_user_id,
            is_admin=is_admin,
            operation=operation,
            filters=filters,
            source_id=source_id,
            project_id=project_id,
            tags=tags,
            extension=extension,
            path_prefix=path_prefix,
            status=status,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
            order_by=order_by,
            order=order,
            group_by=group_by,
        )

    @staticmethod
    async def search(
        session: AsyncSession,
        *,
        query: str,
        actor_user_id: Optional[uuid.UUID],
        is_admin: bool = False,
        filters: Optional[KnowledgeSearchFilters] = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        filters = filters or KnowledgeSearchFilters()
        query = query.strip()
        indexed_hits = await KnowledgeService._search_index(
            session,
            query=query,
            actor_user_id=actor_user_id,
            is_admin=is_admin,
            filters=filters,
            limit=max(limit * 3, limit),
        )
        lexical_hits = await KnowledgeService._search_lexical(
            session,
            query=query,
            actor_user_id=actor_user_id,
            is_admin=is_admin,
            filters=filters,
            limit=max(limit * 3, limit),
        )
        return KnowledgeService._merge_search_hits(indexed_hits, lexical_hits, limit)

    @staticmethod
    async def _search_index(
        session: AsyncSession,
        *,
        query: str,
        actor_user_id: Optional[uuid.UUID],
        is_admin: bool,
        filters: KnowledgeSearchFilters,
        limit: int,
    ) -> list[dict[str, Any]]:
        if not query:
            return []
        readable = await KnowledgeService._readable_source_ids(
            session, source_id=filters.source_id,
            actor_user_id=actor_user_id, is_admin=is_admin,
        )
        if not readable:
            return []
        index_filters = replace(filters, readable_source_ids=tuple(readable))
        try:
            from .index_service import get_knowledge_index_service

            index_hits = await get_knowledge_index_service().search(
                query=query,
                filters=index_filters,
                limit=limit,
            )
        except Exception:
            logger.exception("Knowledge index lookup failed; falling back to lexical search")
            return []
        if not index_hits:
            return []

        chunk_ids = [hit.chunk_id for hit in index_hits]
        result = await session.execute(
            select(KnowledgeChunk, KnowledgeDocument, KnowledgeSource)
            .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
            .join(KnowledgeSource, KnowledgeDocument.source_id == KnowledgeSource.id)
            .where(
                KnowledgeChunk.id.in_(chunk_ids),
                KnowledgeDocument.status == "active",
            )
        )
        rows = {chunk.id: (chunk, document, source) for chunk, document, source in result.all()}
        hits: list[dict[str, Any]] = []
        for index_hit in index_hits:
            row = rows.get(index_hit.chunk_id)
            if not row:
                continue
            chunk, document, source = row
            if not await KnowledgeService._passes_search_acl_and_filters(
                session,
                source=source,
                document=document,
                actor_user_id=actor_user_id,
                is_admin=is_admin,
                filters=filters,
            ):
                continue
            lexical_score = KnowledgeService._lexical_score(query, chunk.text, document)
            hits.append(
                KnowledgeService._search_payload(
                    score=float(index_hit.score) + min(lexical_score, 20.0) * 0.05,
                    source=source,
                    document=document,
                    chunk=chunk,
                    retrieval="hybrid",
                )
            )
            if len(hits) >= limit:
                break
        return hits

    @staticmethod
    async def _search_lexical(
        session: AsyncSession,
        *,
        query: str,
        actor_user_id: Optional[uuid.UUID],
        is_admin: bool = False,
        filters: Optional[KnowledgeSearchFilters] = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        from .lexical import search_lexical

        return await search_lexical(
            session, query=query, actor_user_id=actor_user_id,
            is_admin=is_admin, filters=filters, limit=limit,
        )

    @staticmethod
    async def _passes_search_acl_and_filters(
        session: AsyncSession,
        *,
        source: KnowledgeSource,
        document: KnowledgeDocument,
        actor_user_id: Optional[uuid.UUID],
        is_admin: bool,
        filters: KnowledgeSearchFilters,
    ) -> bool:
        if document.status != "active" or document.source_id != source.id:
            return False
        if filters.source_id and document.source_id != KnowledgeService._coerce_uuid(filters.source_id):
            return False
        if not await KnowledgeService.can_read_source(
            session,
            source_id=source.id,
            actor_user_id=actor_user_id,
            is_admin=is_admin,
        ):
            return False
        if filters.tags:
            doc_tags = {str(tag).lower() for tag in document.tags or []}
            if not all(tag.lower() in doc_tags for tag in filters.tags):
                return False
        if filters.project_id:
            project_refs = {str(ref) for ref in document.project_refs or []}
            if str(filters.project_id) not in project_refs:
                return False
        if filters.extension:
            extension = filters.extension if filters.extension.startswith(".") else f".{filters.extension}"
            if document.extension != extension.lower():
                return False
        if filters.path_prefix and not (document.path or "").lower().startswith(filters.path_prefix.lower()):
            return False
        return True

    @staticmethod
    def _search_payload(
        *,
        score: float,
        source: KnowledgeSource,
        document: KnowledgeDocument,
        chunk: KnowledgeChunk,
        retrieval: str,
    ) -> dict[str, Any]:
        return {
            "score": score,
            "retrieval": retrieval,
            "url": KnowledgeService._document_url(source, document),
            "source": source.to_dict(),
            "document": document.to_dict(),
            "chunk": {
                "id": str(chunk.id),
                "heading_path": chunk.heading_path or [],
                "chunk_index": chunk.chunk_index,
                "text": chunk.text,
            },
        }

    @staticmethod
    def _document_url(
        source: KnowledgeSource, document: KnowledgeDocument
    ) -> Optional[str]:
        """検索結果の出典URL。GROWI ソースは Wiki ページURLを返す。"""
        if source.source_type == "growi":
            return build_page_url(source.root_path, document.path)
        return None

    @staticmethod
    def _merge_search_hits(
        indexed_hits: list[dict[str, Any]],
        lexical_hits: list[dict[str, Any]],
        limit: int,
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for hit in [*indexed_hits, *lexical_hits]:
            chunk_id = hit.get("chunk", {}).get("id")
            if not chunk_id:
                continue
            existing = merged.get(chunk_id)
            if existing is None or float(hit.get("score", 0.0)) > float(existing.get("score", 0.0)):
                merged[chunk_id] = hit
                continue
            if existing.get("retrieval") != hit.get("retrieval"):
                existing["retrieval"] = "hybrid"
        return sorted(merged.values(), key=lambda item: item["score"], reverse=True)[:limit]

    @staticmethod
    def _lexical_score(query: str, text: str, document: KnowledgeDocument) -> float:
        if not query:
            return 0.0
        lower_text = text.lower()
        title = (document.title or "").lower()
        path = (document.path or "").lower()
        score = 0.0
        for term in re.split(r"\s+", query.lower()):
            if not term:
                continue
            score += lower_text.count(term)
            if term in title:
                score += 4
            if term in path:
                score += 2
        return score

    @staticmethod
    async def read_document(
        session: AsyncSession,
        *,
        actor_user_id: Optional[uuid.UUID],
        is_admin: bool = False,
        document_id: Optional[uuid.UUID] = None,
        source_id: Optional[uuid.UUID] = None,
        path: Optional[str] = None,
    ) -> dict[str, Any]:
        if document_id:
            result = await session.execute(
                select(KnowledgeDocument, KnowledgeSource)
                .join(KnowledgeSource, KnowledgeDocument.source_id == KnowledgeSource.id)
                .where(KnowledgeDocument.id == document_id)
            )
        elif source_id and path:
            result = await session.execute(
                select(KnowledgeDocument, KnowledgeSource)
                .join(KnowledgeSource, KnowledgeDocument.source_id == KnowledgeSource.id)
                .where(
                    KnowledgeDocument.source_id == source_id,
                    KnowledgeDocument.path == path.replace("\\", "/"),
                )
            )
        else:
            raise ValueError("document_id または source_id/path が必要です")
        row = result.first()
        if not row:
            raise ValueError("ドキュメントが見つかりません")
        document, source = row
        if not await KnowledgeService.can_read_source(
            session,
            source_id=source.id,
            actor_user_id=actor_user_id,
            is_admin=is_admin,
        ):
            raise PermissionError("ドキュメントの閲覧権限がありません")
        if source.source_type == "growi":
            # GROWI は正本がファイルシステムに無いため、保持済みチャンクから本文を再構成する。
            chunk_result = await session.execute(
                select(KnowledgeChunk)
                .where(KnowledgeChunk.document_id == document.id)
                .order_by(KnowledgeChunk.chunk_index.asc())
            )
            text = "\n\n".join(
                chunk.text for chunk in chunk_result.scalars().all() if chunk.text
            )
            return {
                "source": source.to_dict(),
                "document": document.to_dict(),
                "content": text,
                "error": None,
                "url": build_page_url(source.root_path, document.path),
            }
        root = lexical_path(source.root_path)
        file_path = root.joinpath(*relative_parts(document.path))
        text, error, _stat = await read_source_file(
            root, file_path, KnowledgeService._read_file_text,
        )
        return {
            "source": source.to_dict(),
            "document": document.to_dict(),
            "content": text,
            "error": error,
        }

    @staticmethod
    async def outline(
        session: AsyncSession,
        *,
        actor_user_id: Optional[uuid.UUID],
        is_admin: bool = False,
        document_id: Optional[uuid.UUID] = None,
        source_id: Optional[uuid.UUID] = None,
        path: Optional[str] = None,
    ) -> dict[str, Any]:
        payload = await KnowledgeService.read_document(
            session,
            actor_user_id=actor_user_id,
            is_admin=is_admin,
            document_id=document_id,
            source_id=source_id,
            path=path,
        )
        headings = []
        for line_no, line in enumerate(payload["content"].splitlines(), start=1):
            match = _HEADING_RE.match(line)
            if match:
                headings.append(
                    {"level": len(match.group(1)), "title": match.group(2).strip(), "line": line_no}
                )
        payload["outline"] = headings
        payload.pop("content", None)
        return payload

    @staticmethod
    async def organize(
        session: AsyncSession,
        *,
        source_id: uuid.UUID,
        actor_user_id: Optional[uuid.UUID],
        is_admin: bool = False,
        dry_run: bool = True,
        limit: int = 200,
    ) -> dict[str, Any]:
        if not await KnowledgeService.can_read_source(
            session,
            source_id=source_id,
            actor_user_id=actor_user_id,
            is_admin=is_admin,
        ):
            raise PermissionError("ナレッジソースの閲覧権限がありません")
        result = await session.execute(
            select(KnowledgeDocument)
            .options(selectinload(KnowledgeDocument.outgoing_links))
            .where(
                KnowledgeDocument.source_id == source_id,
                KnowledgeDocument.status == "active",
            )
            .order_by(KnowledgeDocument.path)
            .limit(limit)
        )
        documents = list(result.scalars().all())
        suggestions: list[dict[str, Any]] = []

        all_paths = {doc.path for doc in documents}
        all_stems = {Path(doc.path).stem for doc in documents}

        for document in documents:
            doc_suggestions = KnowledgeService._suggest_annotations(
                document, all_paths=all_paths, all_stems=all_stems
            )
            for suggestion in doc_suggestions:
                payload = {
                    "document_id": str(document.id),
                    "path": document.path,
                    "annotation_type": suggestion["annotation_type"],
                    "content": suggestion["content"],
                    "confidence": suggestion.get("confidence", 0.7),
                    "source": suggestion.get("source", "rule"),
                    "status": "proposed",
                }
                suggestions.append(payload)
                if not dry_run:
                    session.add(
                        KnowledgeAnnotation(
                            document_id=document.id,
                            annotation_type=payload["annotation_type"],
                            content_json=payload["content"],
                            confidence=payload["confidence"],
                            source=payload["source"],
                            status="proposed",
                            actor_user_id=actor_user_id,
                        )
                    )

        return {
            "source_id": str(source_id),
            "dry_run": dry_run,
            "documents_checked": len(documents),
            "suggestion_count": len(suggestions),
            "suggestions": suggestions,
        }

    @staticmethod
    def _suggest_annotations(
        document: KnowledgeDocument,
        *,
        all_paths: set[str],
        all_stems: set[str],
    ) -> list[dict[str, Any]]:
        suggestions: list[dict[str, Any]] = []
        frontmatter = document.frontmatter_json or {}
        tags = [str(tag).lower() for tag in document.tags or []]
        if not frontmatter.get("title") and (document.extension or "").lower() == ".md":
            suggestions.append(
                {
                    "annotation_type": "warning",
                    "content": {
                        "kind": "missing_title",
                        "message": "frontmatter.title がありません",
                        "suggested_title": document.title or Path(document.path).stem,
                    },
                    "confidence": 0.9,
                }
            )
        if not tags or "anything" in tags:
            suggestions.append(
                {
                    "annotation_type": "classification",
                    "content": {
                        "kind": "tag_suggestion",
                        "current_tags": document.tags or [],
                        "suggested_tags": KnowledgeService._tag_candidates(document),
                    },
                    "confidence": 0.65,
                }
            )
        if document.error_message:
            suggestions.append(
                {
                    "annotation_type": "warning",
                    "content": {
                        "kind": "extract_error",
                        "message": document.error_message,
                    },
                    "confidence": 1.0,
                }
            )
        for link in document.__dict__.get("outgoing_links", []) or []:
            target = link.target_path_or_url
            if target.startswith(("http://", "https://")):
                continue
            normalized = target.replace("\\", "/").strip()
            stem = Path(normalized).stem
            if normalized not in all_paths and stem not in all_stems:
                suggestions.append(
                    {
                        "annotation_type": "warning",
                        "content": {
                            "kind": "broken_link",
                            "target": target,
                        },
                        "confidence": 0.75,
                    }
                )
        return suggestions

    @staticmethod
    def _tag_candidates(document: KnowledgeDocument) -> list[str]:
        text = f"{document.path} {document.title or ''}".lower()
        candidates: list[str] = []
        for keyword, tag in (
            ("meeting", "meeting"),
            ("minutes", "meeting"),
            ("議事", "meeting"),
            ("todo", "todo"),
            ("task", "task"),
            ("設計", "design"),
            ("design", "design"),
            ("決定", "decision"),
            ("decision", "decision"),
        ):
            if keyword in text and tag not in candidates:
                candidates.append(tag)
        return candidates or ["note"]

    @staticmethod
    async def propose_text_replacement(
        session: AsyncSession,
        *,
        document_id: uuid.UUID,
        actor_user_id: Optional[uuid.UUID],
        is_admin: bool = False,
        replacement_content: str,
        reason: str,
    ) -> KnowledgeEditEvent:
        payload = await KnowledgeService.read_document(
            session,
            actor_user_id=actor_user_id,
            is_admin=is_admin,
            document_id=document_id,
        )
        source = payload["source"]
        if not await KnowledgeService.can_write_source(
            session,
            source_id=uuid.UUID(source["id"]),
            actor_user_id=actor_user_id,
            is_admin=is_admin,
        ):
            raise PermissionError("ドキュメントの編集権限がありません")
        current = payload["content"]
        diff = "\n".join(
            difflib.unified_diff(
                current.splitlines(),
                replacement_content.splitlines(),
                fromfile=payload["document"]["path"],
                tofile=payload["document"]["path"],
                lineterm="",
            )
        )
        event = KnowledgeEditEvent(
            document_id=document_id,
            actor_user_id=actor_user_id,
            operation="replace_text",
            diff=diff,
            reason=reason,
            status="proposed",
            pre_hash=KnowledgeService._content_hash(current),
            post_hash=KnowledgeService._content_hash(replacement_content),
        )
        session.add(event)
        return event
