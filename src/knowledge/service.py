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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml
from sqlalchemy import and_, delete, func, select
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
    ProjectMember,
)
from .growi_client import build_page_url


TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".xml", ".log"}
OFFICE_EXTENSIONS = {".docx", ".xlsx", ".pptx", ".pdf"}
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | OFFICE_EXTENSIONS
DEFAULT_INCLUDE_PATTERNS = ["*.md", "*.txt", "*.pdf", "*.docx", "*.xlsx", "*.pptx"]
DEFAULT_EXCLUDE_PATTERNS = [".*", "__pycache__", "node_modules", ".git"]
# GROWI 取り込みで既定除外する Wiki パス（ゴミ箱・個人ページ）。
DEFAULT_GROWI_EXCLUDE_PATTERNS = ["/trash/*", "/trash", "/user/*"]

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
        root = KnowledgeService.resolve_root_path(root_path)
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
        root = KnowledgeService.resolve_root_path(root_path)
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
            )
            .limit(1)
        )
        if user_perm.scalar_one_or_none():
            return True
        project_perm = await session.execute(
            select(KnowledgeSourcePermission.id)
            .join(
                ProjectMember,
                KnowledgeSourcePermission.project_id == ProjectMember.project_id,
            )
            .where(
                KnowledgeSourcePermission.source_id == source_id,
                ProjectMember.user_id == actor_user_id,
            )
            .limit(1)
        )
        return project_perm.scalar_one_or_none() is not None

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
                        return parsed, body
                except Exception:
                    return {}, body
        return {}, text

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
        root = KnowledgeService.resolve_root_path(source.root_path)

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

        for path in sorted(root.rglob("*"), key=lambda item: str(item).lower()):
            if indexed >= max_files:
                break
            if not path.is_file():
                continue
            if not KnowledgeService._matches_patterns(path, root, include, exclude):
                continue

            rel_path = path.relative_to(root).as_posix()
            seen_paths.add(rel_path)
            indexed += 1
            text, extract_error = KnowledgeService._read_file_text(path)
            stat = path.stat()
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
            document.resolved_absolute_path = str(path.resolve())
            document.title = KnowledgeService._extract_title(path, frontmatter, body)
            document.extension = path.suffix.lower()
            document.mime_type = mimetypes.guess_type(path.name)[0]
            document.content_hash = digest
            document.modified_at = datetime.fromtimestamp(stat.st_mtime)
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

        for rel_path, document in existing.items():
            if rel_path not in seen_paths and document.status != "deleted":
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

        for page in sorted(pages, key=lambda item: item.path.lower()):
            if indexed >= max_files:
                break
            if not KnowledgeService._growi_path_matches(page.path, include, exclude):
                continue

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
            document.modified_at = KnowledgeService._parse_iso_datetime(page.updated_at)
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
            if rel_path not in seen_paths and document.status != "deleted":
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
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(
                tzinfo=None
            )
        except ValueError:
            return None

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
        try:
            from .index_service import get_knowledge_index_service

            index_hits = await get_knowledge_index_service().search(
                query=query,
                filters=filters,
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
        filters = filters or KnowledgeSearchFilters()
        query = query.strip()
        terms = [term for term in re.split(r"\s+", query) if term]
        conditions = [KnowledgeDocument.status == "active"]
        if filters.source_id:
            conditions.append(KnowledgeDocument.source_id == filters.source_id)
        if filters.extension:
            extension = filters.extension
            if not extension.startswith("."):
                extension = f".{extension}"
            conditions.append(KnowledgeDocument.extension == extension.lower())
        if filters.path_prefix:
            conditions.append(KnowledgeDocument.path.ilike(f"{filters.path_prefix}%"))
        candidate_limit = max(limit * 50, 500) if query else max(limit * 5, limit)

        result = await session.execute(
            select(KnowledgeChunk, KnowledgeDocument, KnowledgeSource)
            .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
            .join(KnowledgeSource, KnowledgeDocument.source_id == KnowledgeSource.id)
            .where(and_(*conditions))
            .order_by(KnowledgeDocument.updated_at.desc().nullslast())
            .limit(candidate_limit)
        )

        hits: list[dict[str, Any]] = []
        lower_terms = [term.lower() for term in terms]
        for chunk, document, source in result.all():
            if not await KnowledgeService.can_read_source(
                session,
                source_id=source.id,
                actor_user_id=actor_user_id,
                is_admin=is_admin,
            ):
                continue
            if filters.tags:
                doc_tags = {str(tag).lower() for tag in document.tags or []}
                if not all(tag.lower() in doc_tags for tag in filters.tags):
                    continue
            if filters.project_id:
                project_refs = {str(ref) for ref in document.project_refs or []}
                if str(filters.project_id) not in project_refs:
                    continue
            if query:
                haystack = "\n".join(
                    [
                        chunk.text or "",
                        document.title or "",
                        document.path or "",
                    ]
                ).lower()
                if not all(term in haystack for term in lower_terms):
                    continue
            score = KnowledgeService._lexical_score(query, chunk.text, document)
            hits.append(
                KnowledgeService._search_payload(
                    score=score,
                    source=source,
                    document=document,
                    chunk=chunk,
                    retrieval="lexical",
                )
            )
            if len(hits) >= limit:
                break
        return sorted(hits, key=lambda item: item["score"], reverse=True)

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
        file_path = Path(source.root_path).resolve() / document.path
        resolved = file_path.resolve()
        root = Path(source.root_path).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise PermissionError("ナレッジソース外のファイルは読めません") from exc
        text, error = KnowledgeService._read_file_text(resolved)
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
