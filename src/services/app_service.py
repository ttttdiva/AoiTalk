"""Application service layer for persistent Apps."""

from __future__ import annotations

import logging
import os
import shutil
import stat
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence
from uuid import UUID

from sqlalchemy import and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import (
    App,
    AppGrant,
    AppTarget,
    KnowledgeNode,
    KnowledgeSearchIndex,
    DocsLibrary,
    Project,
    ProjectApp,
    ProjectMember,
)
from .app_manifest_service import sync_manifest_targets
from .app_storage import (
    ensure_app_workspace,
    get_app_workspace_path,
    resolve_workspace_file,
    sha256_file,
)
from .app_git_service import AppGitError, AppGitService
from .docs_workspace import ensure_docs_library
from .project_permissions import normalize_project_member_permissions


logger = logging.getLogger(__name__)


APP_DOCS_ROOT_SYSTEM_KEY = "apps_root"
APP_DOCS_ROOT_TITLE = "アプリ"


PERMISSION_RANK = {
    "viewer": 10,
    "runner": 20,
    "developer": 30,
    "maintainer": 40,
    "admin": 50,
}


class AppAccessError(PermissionError):
    """App permission failure."""


def permission_at_least(actual: str | None, required: str) -> bool:
    return PERMISSION_RANK.get(str(actual or ""), 0) >= PERMISSION_RANK[required]


async def _resolve_user_role(
    session: AsyncSession,
    user_id: UUID,
    user_role: str | None,
) -> str | None:
    """Resolve a missing role from the authenticated principal, fail-closed."""
    if user_role is not None:
        return user_role
    getter = getattr(session, "get", None)
    if getter is None:
        return None
    from ..memory.models import User

    principal = await getter(User, user_id)
    return getattr(principal, "role", None)


async def _ensure_app_docs_root(
    session: AsyncSession,
    *,
    docs_library_id: UUID,
    user_id: UUID | None,
) -> KnowledgeNode:
    """Ensure the single system hub that owns all App README nodes.

    App README nodes are global App records, not Project records.  Keeping a
    stable workspace-level parent prevents every App name from becoming a Docs
    root while still allowing one App to be bound to multiple Projects.
    """
    bind = session.get_bind()
    if getattr(getattr(bind, "dialect", None), "name", None) == "postgresql":
        await session.execute(
            text("select pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": f"{docs_library_id}:apps-root"},
        )

    result = await session.execute(
        select(KnowledgeNode)
        .where(
            KnowledgeNode.docs_library_id == docs_library_id,
            KnowledgeNode.system_key == APP_DOCS_ROOT_SYSTEM_KEY,
        )
        .limit(1)
    )
    root = result.scalar_one_or_none()
    if root is None:
        from .docs_graph_service import DocsGraphService

        root = await DocsGraphService(session).create_node(
            docs_library_id=docs_library_id,
            user_id=user_id,
            title=APP_DOCS_ROOT_TITLE,
            system_key=APP_DOCS_ROOT_SYSTEM_KEY,
            body_json={"format": "app_collection"},
            node_type="system",
            sort_order=3,
        )

    needs_normalization = (
        root.title != APP_DOCS_ROOT_TITLE
        or root.body_text != APP_DOCS_ROOT_TITLE
        or root.body_json != {"format": "app_collection"}
        or root.parent_id is not None
        or root.root_page_id != root.id
        or root.project_id is not None
        or root.archived_at is not None
    )
    if needs_normalization:
        root.title = APP_DOCS_ROOT_TITLE
        root.body_text = APP_DOCS_ROOT_TITLE
        root.body_json = {"format": "app_collection"}
        root.parent_id = None
        root.root_page_id = root.id
        root.project_id = None
        root.archived_at = None
        root.updated_by = user_id
        root.updated_at = datetime.utcnow()
    from .docs_graph_service import DocsGraphService

    search_index = await session.get(KnowledgeSearchIndex, root.id)
    index_matches = bool(
        search_index
        and search_index.docs_library_id == root.docs_library_id
        and search_index.project_id == root.project_id
        and search_index.title_text == root.title
        and search_index.body_text_plain == root.body_text
    )
    if needs_normalization or not index_matches:
        await DocsGraphService(session).upsert_search_index(root)
    await session.flush()
    return root


def _force_remove_tree(path: Path) -> bool:
    """Remove an App workspace even when Git left read-only objects behind.

    Windows では ``.git/objects`` が read-only 属性で作られるため、素の
    ``shutil.rmtree`` は ``PermissionError`` になり、``ignore_errors=True``
    だと「補償したつもりで workspace がまるごと残る」。書き込み属性を戻して
    再試行し、実際に消えたかどうかを返す。
    """
    if not path.exists():
        return True

    def _retry(func, target, _exception=None):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            logger.warning("App workspace cleanup failed: %s", target)

    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_retry)
        else:  # pragma: no cover - 3.11 以前の互換
            shutil.rmtree(path, onerror=lambda func, target, info: _retry(func, target))
    except OSError:
        logger.exception("App workspace cleanup failed: %s", path)
    return not path.exists()


def _permission_from_grants(
    *,
    app: App,
    user_id: UUID,
    user_role: str | None,
    project_id: UUID | None,
    grants: Sequence[AppGrant],
    has_enabled_project_binding: bool,
) -> str | None:
    """Decide one App permission from already-loaded rows.

    ``permission_for_app``（1件ずつ）と ``list_accessible_apps``（一括先読み）で
    同じ判定を共有するための純関数。Project スコープの認可（``project_access``）は
    呼び出し側で先に済ませる前提。
    """
    if user_role == "admin" or app.owner_user_id == user_id:
        return "admin"
    best = max(
        (grant.permission for grant in grants if grant.user_id == user_id),
        key=lambda value: PERMISSION_RANK.get(value, 0),
        default=None,
    )
    if project_id:
        project_permissions = [
            grant.permission for grant in grants if grant.project_id == project_id
        ]
        if project_permissions:
            candidate = max(project_permissions, key=lambda value: PERMISSION_RANK.get(value, 0))
            if PERMISSION_RANK.get(candidate, 0) > PERMISSION_RANK.get(best or "", 0):
                best = candidate
        elif has_enabled_project_binding:
            best = best or "viewer"
    if best:
        return best
    if app.visibility == "public":
        return "viewer"
    return None


def validate_capability_grants(
    grants: dict[str, Any] | None,
    targets: Iterable[AppTarget],
) -> set[str]:
    """Validate ProjectApp grants against Manifest-declared capabilities.

    The JSON shape is intentionally permissive for existing clients: callers
    may send ``{"capabilities": [...]}``, ``{"grants": [...]}``,
    ``{"target_key": [...]}``, or a boolean map such as
    ``{"project.files.read": true}``. Every granted capability must be
    declared by at least one Manifest target; execution/bridge code then uses
    the intersection for the selected target.
    """
    if grants is None:
        return set()
    if not isinstance(grants, dict):
        raise ValueError("capability_grants_json は object で指定してください")

    target_map = {}
    for target in targets:
        snapshot = target.manifest_snapshot if isinstance(target.manifest_snapshot, dict) else {}
        target_map[target.target_key] = {
            str(item)
            for item in snapshot.get("capabilities", [])
            if isinstance(item, str)
        }
    declared = set().union(*target_map.values()) if target_map else set()
    requested: set[str] = set()

    def add_values(value: Any) -> None:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError("capability grants は文字列配列で指定してください")
        requested.update(item.strip() for item in value if item.strip())

    for key, value in grants.items():
        key = str(key)
        if key in {"capabilities", "grants"}:
            add_values(value)
        elif key in target_map:
            before = set(requested)
            add_values(value)
            target_requested = requested - before
            if not target_requested.issubset(target_map[key]):
                unknown = sorted(target_requested - target_map[key])
                raise ValueError(f"Target {key} に未宣言のCapabilityがあります: {', '.join(unknown)}")
        elif isinstance(value, bool):
            if value:
                requested.add(key)
        elif isinstance(value, list):
            add_values(value)
        else:
            raise ValueError("capability grants の値が不正です")

    unknown = sorted(requested - declared)
    if unknown:
        raise ValueError(f"Manifestに未宣言のCapabilityがあります: {', '.join(unknown)}")
    return requested


class AppService:
    """DB, workspace and permission operations shared by API and tools."""

    def __init__(self, *, workspace_root: str | Path | None = None) -> None:
        self.workspace_root = workspace_root

    async def create_app(
        self,
        session: AsyncSession,
        *,
        owner_user_id: UUID,
        name: str,
        slug: str,
        description: str = "",
        origin_project_id: UUID | None = None,
        visibility: str = "private",
    ) -> App:
        app = App(
            owner_user_id=owner_user_id,
            origin_project_id=origin_project_id,
            name=name.strip(),
            slug=slug.strip(),
            description=description.strip() or None,
            visibility=visibility,
        )
        session.add(app)
        await session.flush()
        # workspace 作成後にここで失敗すると App ID がまだ呼び出し元へ返って
        # おらず、ルート側からは孤児 workspace を特定できない。自分で補償する。
        workspace_path = get_app_workspace_path(app.id, workspace_root=self.workspace_root)
        try:
            workspace = ensure_app_workspace(
                app.id,
                name=app.name,
                description=app.description or "",
                workspace_root=self.workspace_root,
            )
            await sync_manifest_targets(session, app, workspace)
            await self.ensure_readme_node(session, app, owner_user_id, workspace=workspace)
            await session.flush()
            # 初期 README / Manifest / scaffold を App Git の最初の revision として固定する。
            try:
                AppGitService(workspace_root=self.workspace_root).checkpoint(
                    app.id,
                    "App workspace を初期化",
                    actor=str(owner_user_id),
                )
            except AppGitError:
                # Gitは可搬インストールで任意依存のため、App CRUD自体は継続する。
                pass
        except BaseException:
            if not _force_remove_tree(workspace_path):
                logger.warning(
                    "App作成失敗時のworkspace削除に失敗しました: app_id=%s path=%s",
                    app.id,
                    workspace_path,
                )
            raise
        return app

    async def ensure_readme_node(
        self,
        session: AsyncSession,
        app: App,
        owner_user_id: UUID,
        *,
        workspace: Path | None = None,
    ) -> KnowledgeNode:
        workspace = workspace or get_app_workspace_path(app.id, workspace_root=self.workspace_root)
        readme_path = resolve_workspace_file(workspace, "README.md")
        readme = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""
        readme_hash = sha256_file(readme_path) if readme_path.exists() else None
        system_key = f"app:{app.id}:readme"
        docs_workspace = await ensure_docs_library(session, owner_user_id=owner_user_id)
        node = None
        if app.readme_node_id:
            candidate = await session.get(KnowledgeNode, app.readme_node_id)
            if candidate is not None and candidate.docs_library_id == docs_workspace.id and candidate.system_key == system_key and candidate.app_id in {None, app.id}:
                node = candidate
        if node is None:
            node = await session.scalar(
                select(KnowledgeNode)
                .where(
                    KnowledgeNode.docs_library_id == docs_workspace.id,
                    KnowledgeNode.system_key == system_key,
                    or_(KnowledgeNode.app_id == app.id, KnowledgeNode.app_id.is_(None)),
                )
                .limit(1)
            )
        if node is None:
            app_root = await _ensure_app_docs_root(
                session,
                docs_library_id=docs_workspace.id,
                user_id=owner_user_id,
            )
            node = KnowledgeNode(
                docs_library_id=docs_workspace.id,
                parent_id=app_root.id,
                root_page_id=app_root.id,
                system_key=system_key,
                title=app.name,
                description=app.description or "",
                body_text=readme,
                body_json={"markdown": readme},
                node_type="app_readme",
                display_props={
                    "app_id": str(app.id),
                    "canonical_file": "README.md",
                    "app_readme_sha256": readme_hash,
                },
                app_id=app.id,
                created_by=owner_user_id,
                updated_by=owner_user_id,
            )
            session.add(node)
        else:
            app_root = await _ensure_app_docs_root(
                session,
                docs_library_id=node.docs_library_id,
                user_id=owner_user_id,
            )
            node.title = app.name
            node.description = app.description or ""
            node.body_text = readme
            node.body_json = {"markdown": readme}
            node.parent_id = app_root.id
            node.root_page_id = app_root.id
            node.project_id = None
            node.app_id = app.id
            node.display_props = {
                **(node.display_props if isinstance(node.display_props, dict) else {}),
                "app_id": str(app.id),
                "canonical_file": "README.md",
                "app_readme_sha256": readme_hash,
            }
            node.updated_by = owner_user_id
            node.updated_at = datetime.utcnow()
        await session.flush()
        from .docs_graph_service import DocsGraphService

        graph = DocsGraphService(session)
        await graph._propagate_root_page(node)
        await graph.upsert_search_index(node)
        await session.flush()
        app.readme_node_id = node.id
        return node

    async def sync_readme_to_node(
        self,
        session: AsyncSession,
        app: App,
        actor_user_id: UUID,
    ) -> KnowledgeNode:
        node = await self.ensure_readme_node(session, app, actor_user_id)
        # README.md is the canonical file, but Docs search/revision consumers
        # must observe the same write transaction.
        from .docs_graph_service import DocsGraphService

        await DocsGraphService(session).record_node_change(
            node,
            actor_user_id,
            "App README を同期",
            source_refs=[{"source": "app_readme", "app_id": str(app.id)}],
        )
        return node

    async def get_app(self, session: AsyncSession, app_id: UUID) -> App | None:
        return await session.scalar(select(App).where(App.id == app_id).limit(1))

    async def get_project_member(
        self, session: AsyncSession, project_id: UUID, user_id: UUID
    ) -> ProjectMember | None:
        return await session.scalar(
            select(ProjectMember)
            .where(and_(ProjectMember.project_id == project_id, ProjectMember.user_id == user_id))
            .limit(1)
        )

    async def project_access(
        self,
        session: AsyncSession,
        *,
        project_id: UUID,
        user_id: UUID,
        user_role: str | None = None,
    ) -> bool:
        user_role = await _resolve_user_role(session, user_id, user_role)
        project = await session.scalar(
            select(Project)
            .where(Project.id == project_id, Project.deleted_at.is_(None))
            .limit(1)
        )
        if not project:
            return False
        if user_role == "admin":
            return True
        if project.owner_id == user_id:
            return True
        member = await self.get_project_member(session, project_id, user_id)
        if member is None:
            return False
        permissions = normalize_project_member_permissions(member.permissions)
        # Membership alone is not a read grant.  Keep the App service aligned
        # with project_context.has_project_read_access(): legacy rows with an
        # empty permissions object (or write-only permissions) fail closed.
        return permissions.get("read") is True

    async def project_write_access(
        self,
        session: AsyncSession,
        *,
        project_id: UUID,
        user_id: UUID,
        user_role: str | None = None,
    ) -> bool:
        """Return whether the caller may change a Project's App binding.

        Reading a Project and changing which Apps it exposes are deliberately
        separate capabilities.  App grants (for example ``runner``) do not
        turn a read-only Project member into a Project administrator.
        """
        user_role = await _resolve_user_role(session, user_id, user_role)
        project = await session.scalar(
            select(Project)
            .where(Project.id == project_id, Project.deleted_at.is_(None))
            .limit(1)
        )
        if not project:
            return False
        if user_role == "admin" or project.owner_id == user_id:
            return True
        member = await self.get_project_member(session, project_id, user_id)
        if member is None:
            return False
        permissions = normalize_project_member_permissions(member.permissions)
        return permissions.get("write") is True

    async def permission_for_app(
        self,
        session: AsyncSession,
        app: App,
        *,
        user_id: UUID,
        user_role: str | None = None,
        project_id: UUID | None = None,
    ) -> str | None:
        user_role = await _resolve_user_role(session, user_id, user_role)
        # A Project-scoped App request is also a Project-scoped authorization
        # request.  Do this check before owner/admin/public fallback so a
        # globally visible App cannot be used as a side door into a private
        # Project's Chat, Tasks, Docs, or App instance.
        #
        # App の所有者であってもこのゲートは通さない。Project に紐づいた App の
        # 操作は、その Project の instance / logs / Chat 文脈に触れる操作であり、
        # 非メンバーに開けてはいけない。所有者は project_id を伴わない App
        # スコープ（App 詳細・Manifest・Release・Job）では従来どおり admin 相当
        # で操作できるため、自分の App から締め出されるわけではない。
        # ``execute_app_job`` も別途 ``project_access`` を検証しており、ここだけ
        # 緩めても Project 実行は解禁されない（側面口だけが開く）。
        if project_id is not None and not await self.project_access(
            session,
            project_id=project_id,
            user_id=user_id,
            user_role=user_role,
        ):
            return None
        if user_role == "admin" or app.owner_user_id == user_id:
            # grants を読む前に確定するので、ここで返して1クエリ節約する。
            return "admin"
        grants = list(
            (
                await session.scalars(
                    select(AppGrant).where(
                        and_(
                            AppGrant.app_id == app.id,
                            or_(AppGrant.user_id == user_id, AppGrant.project_id == project_id),
                        )
                    )
                )
            ).all()
        )
        has_enabled_project_binding = False
        if project_id and not any(grant.project_id == project_id for grant in grants):
            # Project grant があるときは binding を見ないので、その場合だけ引く。
            has_enabled_project_binding = bool(
                await session.scalar(
                    select(ProjectApp).where(
                        and_(
                            ProjectApp.app_id == app.id,
                            ProjectApp.project_id == project_id,
                            ProjectApp.enabled.is_(True),
                        )
                    ).limit(1)
                )
            )
        return _permission_from_grants(
            app=app,
            user_id=user_id,
            user_role=user_role,
            project_id=project_id,
            grants=grants,
            has_enabled_project_binding=has_enabled_project_binding,
        )

    async def require_permission(
        self,
        session: AsyncSession,
        app: App,
        *,
        user_id: UUID,
        required: str,
        user_role: str | None = None,
        project_id: UUID | None = None,
        allow_archived: bool = False,
    ) -> str:
        if app.archived_at is not None and not allow_archived:
            raise AppAccessError("アーカイブ済みAppは操作できません")
        actual = await self.permission_for_app(
            session,
            app,
            user_id=user_id,
            user_role=user_role,
            project_id=project_id,
        )
        if not permission_at_least(actual, required):
            raise AppAccessError("Appへの権限がありません")
        return actual or "viewer"

    async def list_accessible_apps(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        user_role: str | None = None,
        project_id: UUID | None = None,
    ) -> list[tuple[App, str]]:
        """Resolve permissions for every visible App with a fixed query count.

        判定内容は ``permission_for_app`` と同一だが、App 1件ずつ grant /
        binding / Project を引くと App 数に比例してクエリが増えるため、
        必要な行を先に一括取得して dict 引きで解決する。
        """
        user_role = await _resolve_user_role(session, user_id, user_role)
        apps = list((await session.scalars(select(App).where(App.archived_at.is_(None)).order_by(App.updated_at.desc()))).all())
        if not apps:
            return []
        if project_id is not None and not await self.project_access(
            session,
            project_id=project_id,
            user_id=user_id,
            user_role=user_role,
        ):
            # Project スコープの認可が無ければ permission_for_app は全App で
            # None を返す。App 単位の判定と同じ結果を1回の判定で確定させる。
            return []
        app_ids = [app.id for app in apps]
        grants_by_app: dict[UUID, list[AppGrant]] = {}
        for grant in (
            await session.scalars(
                select(AppGrant).where(
                    and_(
                        AppGrant.app_id.in_(app_ids),
                        or_(AppGrant.user_id == user_id, AppGrant.project_id == project_id),
                    )
                )
            )
        ).all():
            grants_by_app.setdefault(grant.app_id, []).append(grant)
        bound_app_ids: set[UUID] = set()
        if project_id is not None:
            bound_app_ids = set(
                (
                    await session.scalars(
                        select(ProjectApp.app_id).where(
                            and_(
                                ProjectApp.project_id == project_id,
                                ProjectApp.app_id.in_(app_ids),
                                ProjectApp.enabled.is_(True),
                            )
                        )
                    )
                ).all()
            )
        result: list[tuple[App, str]] = []
        for app in apps:
            permission = _permission_from_grants(
                app=app,
                user_id=user_id,
                user_role=user_role,
                project_id=project_id,
                grants=grants_by_app.get(app.id, ()),
                has_enabled_project_binding=app.id in bound_app_ids,
            )
            if permission:
                result.append((app, permission))
        return result


__all__ = [
    "AppAccessError",
    "AppService",
    "PERMISSION_RANK",
    "permission_at_least",
    "validate_capability_grants",
]
