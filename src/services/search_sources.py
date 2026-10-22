"""Live ACL and original-file resolution shared by media search and AI reads.

An index locator is never authority. Every candidate and every byte retrieval
resolves the current DB row, Project/Space boundary and storage trust boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
import stat
import tempfile
from typing import Any, AsyncIterator
from types import SimpleNamespace
from urllib.parse import quote
import uuid

from sqlalchemy import select, and_, or_, literal, func

from ..memory.models import (
    ConversationMessage,
    ConversationSession,
    ConversationParticipant,
    GeneratedMedia,
    TaskAttachment,
    Task,
    KnowledgeAttachment,
    KnowledgeNode,
    KnowledgeDocument,
    KnowledgeSource,
    Project,
    MeetingProcessingJob,
    User,
    GenerationOutput,
    MediaAssetRevision,
    ProjectMember,
    Space,
    DocsLibrary,
    KnowledgeNodeShare,
    KnowledgeNodeSupertag,
    KnowledgeSupertag,
    KnowledgeSourcePermission,
    WorkspaceFileEntry,
)
from ..memory.project_repository import ProjectRepository
from .storage_roots import StorageError, assert_no_links, lexical_path, relative_parts
from .project_permissions import has_effective_project_permission, normalize_project_member_permissions
from ..security.field_crypto import decrypt_json_value_if_needed
from .privacy_masking_projection import is_privacy_masking_source

_ACL_BATCH_SIZE = 1000
_ACL_SOURCE_LIMIT = 100000


def _acl_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _acl_batches(values):
    values = list(dict.fromkeys(values))
    for offset in range(0, len(values), _ACL_BATCH_SIZE):
        yield values[offset:offset + _ACL_BATCH_SIZE]


@dataclass(frozen=True)
class SourceRef:
    kind: str
    id: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.id}"


@dataclass(frozen=True)
class SearchScope:
    user_id: uuid.UUID
    project_id: uuid.UUID | None = None
    space_id: uuid.UUID | None = None
    # ``None`` is a meaningful personal partition only when the caller says
    # so explicitly. The historical default remains the all-readable
    # wildcard for callers that do not have a saved conversation context.
    project_scope_restricted: bool = False
    character_name: str | None = None
    character_scope_restricted: bool = False


@dataclass
class ResolvedSource:
    ref: SourceRef
    path: Path | None
    title: str
    text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    lexical_segments: list[dict] = field(default_factory=list)
    _resource: Any = field(default=None, repr=False)


class SourceUnavailable(PermissionError):
    """Unavailable/missing/unauthorized originals share a non-disclosing error."""

    def __init__(self, message="Source unavailable", *, reason="source_unavailable"):
        super().__init__(message)
        self.reason = reason


class SearchSourceResolver:
    def __init__(
        self, session, scope: SearchScope, *, workspace_root: str | Path | None = None
    ):
        self.session = session
        self.scope = scope
        # Search/read-only rehearsal must not create an original storage root.
        self.workspace_root = lexical_path(workspace_root or os.environ.get('AOITALK_WORKSPACES_DIR', './workspaces'))

    def _scope_allows_project(self, project_id) -> bool:
        """Return whether a Project (including personal ``None``) is in scope.

        ``SearchScope(user_id)`` intentionally keeps its legacy all-readable
        behavior. A current saved conversation uses the explicit restricted
        flag so ``project_id=None`` means the personal partition rather than a
        wildcard over every Project.
        """
        restricted = bool(getattr(self.scope, 'project_scope_restricted', False))
        if restricted and project_id != self.scope.project_id:
            return False
        if not restricted and self.scope.project_id is not None and project_id != self.scope.project_id:
            return False
        if project_id is None:
            return self.scope.project_id is None and self.scope.space_id is None
        return True

    @staticmethod
    def _character_key(value) -> str:
        # Conversation search uses this canonical slug helper (including its
        # project-manager legacy alias). Keep read authorization on exactly
        # the same character identity range instead of introducing a second
        # case-folded/alias table here.
        from ..memory.cross_session_memory import _canonical_character_slug
        return _canonical_character_slug(value)

    def _scope_allows_character(self, character_name) -> bool:
        if not bool(getattr(self.scope, 'character_scope_restricted', False)):
            return True
        expected_raw = getattr(self.scope, 'character_name', None)
        actual_raw = character_name
        if not str(expected_raw or '').strip() or not str(actual_raw or '').strip():
            return False
        return self._character_key(expected_raw) == self._character_key(actual_raw)

    async def _acl_rows(self, model, identifiers, *columns):
        """Fresh projections only: never hydrate source bodies or ORM ACL caches."""
        rows = {}
        for batch in _acl_batches(identifiers):
            result = await self.session.execute(select(model.id, *columns).where(model.id.in_(batch)))
            rows.update((row.id, SimpleNamespace(**row._mapping)) for row in result)
        return rows

    async def _acl_projects(self, identifiers, actor):
        """Canonical Project permission and Space read policy, in bounded SQL."""
        projects = {}
        for batch in _acl_batches(identifiers):
            result = await self.session.execute(select(
                Project.id, Project.owner_id, Project.space_id, Project.deleted_at,
                Project.is_completed, Project.knowledge_node_id, ProjectMember.permissions,
                Space.id.label('live_space_id'), Space.owner_id.label('space_owner_id'), Space.slug,
            ).outerjoin(ProjectMember, and_(ProjectMember.project_id == Project.id,
                                           ProjectMember.user_id == actor.id))
              .outerjoin(Space, Space.id == Project.space_id).where(Project.id.in_(batch)))
            projects.update((row.id, SimpleNamespace(**row._mapping)) for row in result)
        for project in projects.values():
            project.read = project.deleted_at is None and has_effective_project_permission(
                user_id=actor.id, user_role=actor.role, project_owner_id=project.owner_id,
                member_permissions=project.permissions, permission='read')
            project.operations_read = project.deleted_at is None and has_effective_project_permission(
                user_id=actor.id, user_role=str(actor.role or '').strip().lower(),
                project_owner_id=project.owner_id, member_permissions=project.permissions, permission='read')
        # A live candidate Project owner/read-member already grants normal
        # Space read. Resolve other owner/member Projects only for remaining
        # Spaces (e.g. legacy role casing accepted by Project, not Space).
        known_member_spaces = {p.space_id for p in projects.values() if p.deleted_at is None
            and (p.owner_id == actor.id or normalize_project_member_permissions(p.permissions).get('read') is True)}
        spaces = {p.space_id for p in projects.values() if p.read and p.space_id is not None
            and p.live_space_id is not None and p.slug != f'inbox-{p.space_owner_id}'
            and p.space_owner_id != actor.id and actor.role != 'admin'
            and p.space_id not in known_member_spaces}
        space_member_read = set(known_member_spaces)
        for batch in _acl_batches(spaces):
            result = await self.session.execute(select(Project.space_id, Project.owner_id, ProjectMember.permissions)
                .outerjoin(ProjectMember, and_(ProjectMember.project_id == Project.id,
                                               ProjectMember.user_id == actor.id))
                .where(Project.space_id.in_(batch), Project.deleted_at.is_(None),
                       or_(Project.owner_id == actor.id, ProjectMember.user_id == actor.id)))
            for row in result:
                if row.owner_id == actor.id or normalize_project_member_permissions(row.permissions).get('read') is True:
                    space_member_read.add(row.space_id)
        for project in projects.values():
            space_read = True
            if project.space_id is not None:
                if project.live_space_id is None:
                    space_read = False
                elif project.slug == f'inbox-{project.space_owner_id}':
                    space_read = project.space_owner_id == actor.id
                else:
                    space_read = (project.space_owner_id == actor.id or actor.role == 'admin'
                                  or project.space_id in space_member_read)
            project.scoped_read = (project.read and space_read
                and self._scope_allows_project(project.id)
                and (self.scope.space_id is None or project.space_id == self.scope.space_id))
        return projects

    async def _acl_knowledge_sources(self, identifiers, actor):
        """Candidate subset of KnowledgeService._readable_source_ids policy."""
        from ..knowledge.service import KNOWLEDGE_SOURCE_READ_PERMISSIONS
        allowed = {}
        for batch in _acl_batches(identifiers):
            result = await self.session.execute(select(
                KnowledgeSource.id, KnowledgeSource.owner_user_id, KnowledgeSource.source_type,
                KnowledgeSourcePermission.user_id, KnowledgeSourcePermission.permission,
                Project.id.label('project_id'), Project.owner_id.label('project_owner_id'),
                ProjectMember.permissions,
            ).outerjoin(KnowledgeSourcePermission, KnowledgeSourcePermission.source_id == KnowledgeSource.id)
             .outerjoin(Project, and_(Project.id == KnowledgeSourcePermission.project_id,
                                     Project.deleted_at.is_(None)))
             .outerjoin(ProjectMember, and_(ProjectMember.project_id == Project.id,
                                            ProjectMember.user_id == actor.id))
             .where(KnowledgeSource.id.in_(batch)))
            rows = list(result)
            projects = await self._acl_projects({row.project_id for row in rows if row.project_id}, actor)
            for row in rows:
                direct = row.user_id == actor.id and row.permission in KNOWLEDGE_SOURCE_READ_PERMISSIONS
                project = (row.project_id is not None
                    and str(row.permission or '').strip().lower() in KNOWLEDGE_SOURCE_READ_PERMISSIONS
                    and row.project_id in projects and projects[row.project_id].scoped_read
                    and has_effective_project_permission(user_id=actor.id, user_role=actor.role,
                        project_owner_id=row.project_owner_id, member_permissions=row.permissions, permission='read'))
                if row.owner_user_id == actor.id or direct or project:
                    allowed[row.id] = row.source_type
        return allowed

    async def _acl_doc_shares(self, nodes, actor):
        """Same-library ancestor shares at depths 0..512, bounded in the DB.

        Parent edges are single-valued. A cycle cannot introduce another
        ancestor after its first traversal; the depth bound preserves the
        point reader's 512-hop limit without materializing an entire library.
        Read and write shares both grant read, so existence of the nearest
        valid share has the same read result as any valid share on this chain.
        """
        allowed = set()
        table = KnowledgeNode.__table__
        for batch in _acl_batches(nodes):
            walk = select(table.c.id.label('target_id'), table.c.id.label('node_id'),
                table.c.parent_id, table.c.docs_library_id, literal(0).label('depth')
            ).where(table.c.id.in_(batch)).cte('media_acl_ancestors', recursive=True)
            parent = table.alias('media_acl_parent')
            walk = walk.union_all(select(walk.c.target_id, parent.c.id, parent.c.parent_id,
                parent.c.docs_library_id, (walk.c.depth + 1)).join(parent,
                    and_(parent.c.id == walk.c.parent_id, parent.c.docs_library_id == walk.c.docs_library_id))
                .where(walk.c.depth < 512))
            result = await self.session.execute(select(walk.c.target_id).distinct()
                .join(KnowledgeNodeShare, KnowledgeNodeShare.node_id == walk.c.node_id)
                .where(KnowledgeNodeShare.user_id == actor.id,
                       func.lower(KnowledgeNodeShare.permission).in_(['read', 'write'])))
            allowed.update(result.scalars())
        return allowed

    async def allowed_refs(self, refs, *, require_local_original=True) -> list[str]:
        """Return live authorized locator keys; no source content or paths.

        SQL scales with kinds and 1000-row batches, not individual sources or
        libraries. Docs ancestry costs at most 513 rows per foreign Personal
        candidate inside its recursive query. Results are never cached across
        calls; result/original readers must still use resolve for final ACL.
        """
        unique = {}
        for ref in refs:
            unique.setdefault(ref.key, ref)
            if len(unique) > _ACL_SOURCE_LIMIT:
                raise SourceUnavailable('Media ACL source limit exceeded (100000)', reason='source_limit_exceeded')
        if not unique:
            return []
        try:
            result = await self.session.execute(select(User.id, User.role, User.is_active).where(User.id == self.scope.user_id))
            row = result.one_or_none()
            if row is None or row.is_active is not True:
                return []
            actor = SimpleNamespace(**row._mapping)
            return await self._allowed_refs(unique, actor, require_local_original=require_local_original)
        except SourceUnavailable:
            raise
        except Exception as exc:
            raise SourceUnavailable() from exc

    async def _allowed_refs(self, unique, actor, *, require_local_original):
        specs = {
            'generation_output': (GenerationOutput, GenerationOutput.owner_user_id, GenerationOutput.project_id),
            'generated_media': (GeneratedMedia, GeneratedMedia.owner_user_id, GeneratedMedia.context_type,
                                GeneratedMedia.context_id, GeneratedMedia.status),
            'chat_attachment': (ConversationMessage, ConversationMessage.session_id, ConversationMessage.deleted_at,
                                ConversationMessage.is_active_branch, ConversationMessage.message_metadata),
            'conversation_message': (ConversationMessage, ConversationMessage.session_id, ConversationMessage.deleted_at,
                                     ConversationMessage.is_active_branch, ConversationMessage.message_metadata),
            'task_attachment': (TaskAttachment, TaskAttachment.task_id, TaskAttachment.project_id),
            'docs_attachment': (KnowledgeAttachment, KnowledgeAttachment.node_id),
            'knowledge_document': (KnowledgeDocument, KnowledgeDocument.source_id, KnowledgeDocument.status,
                                   KnowledgeDocument.project_refs),
            'meeting_audio': (MeetingProcessingJob, MeetingProcessingJob.actor_user_id, MeetingProcessingJob.status,
                              MeetingProcessingJob._request_json.label('request_json')),
            'workspace_file': (WorkspaceFileEntry, WorkspaceFileEntry.scope, WorkspaceFileEntry.owner_id,
                               WorkspaceFileEntry.status, WorkspaceFileEntry.deleted_at),
        }
        parsed, ids = {}, {kind: set() for kind in specs}
        for key, ref in unique.items():
            try:
                parts = ref.id.split(':') if ref.kind in {'chat_attachment', 'conversation_message'} else [ref.id]
                identifier = _acl_uuid(parts[0])
                index = int(parts[1]) if ref.kind == 'chat_attachment' else None
                if ref.kind not in specs or identifier is None or (index is not None and index < 0):
                    continue
                parsed[key] = (ref.kind, identifier, index)
                ids[ref.kind].add(identifier)
            except (ValueError, TypeError, IndexError, AttributeError):
                continue
        rows = {kind: await self._acl_rows(spec[0], ids[kind], *spec[1:]) for kind, spec in specs.items()}
        messages = {**rows['chat_attachment'], **rows['conversation_message']}
        session_ids = {r.session_id for r in messages.values()}
        session_ids.update(ident for r in rows['generated_media'].values() if r.context_type == 'conversation'
                           for ident in [_acl_uuid(r.context_id)] if ident is not None)
        conversations = await self._acl_rows(ConversationSession, session_ids, ConversationSession.user_id,
                                             ConversationSession.project_id, ConversationSession.deleted_at,
                                             ConversationSession.character_name)
        joined = set()
        for batch in _acl_batches(session_ids):
            result = await self.session.execute(select(ConversationParticipant.session_id).where(
                ConversationParticipant.session_id.in_(batch), ConversationParticipant.participant_type == 'user',
                ConversationParticipant.participant_id == str(actor.id), ConversationParticipant.status == 'joined'))
            joined.update(result.scalars())
        tasks = await self._acl_rows(Task, {r.task_id for r in rows['task_attachment'].values()}, Task.project_id, Task.deleted_at)
        nodes = await self._acl_rows(KnowledgeNode, {r.node_id for r in rows['docs_attachment'].values()},
            KnowledgeNode.docs_library_id, KnowledgeNode.project_id, KnowledgeNode.parent_id, KnowledgeNode.root_page_id,
            KnowledgeNode.archived_at, KnowledgeNode.system_key, KnowledgeNode.title, KnowledgeNode.node_type, KnowledgeNode.is_explicit_blank)
        libraries = await self._acl_rows(DocsLibrary, {r.docs_library_id for r in nodes.values()},
                                          DocsLibrary.owner_user_id, DocsLibrary.library_type)
        identities = {r.id for r in nodes.values() if str(r.system_key or '').strip().startswith('project_information:')}
        hubs = {r.id for r in nodes.values() if str(r.system_key or '').strip() == 'project_information_root'}
        parents = await self._acl_rows(KnowledgeNode, {nodes[i].parent_id for i in identities if nodes[i].parent_id is not None},
            KnowledgeNode.docs_library_id, KnowledgeNode.parent_id, KnowledgeNode.root_page_id, KnowledgeNode.archived_at,
            KnowledgeNode.system_key, KnowledgeNode.title)
        pointers, tagged, children = {}, set(), {}
        for batch in _acl_batches(identities):
            result = await self.session.execute(select(Project.id, Project.knowledge_node_id).where(Project.knowledge_node_id.in_(batch)))
            for row in result:
                pointers.setdefault(row.knowledge_node_id, set()).add(row.id)
            result = await self.session.execute(select(KnowledgeNodeSupertag.node_id, KnowledgeSupertag.docs_library_id)
                .join(KnowledgeSupertag, KnowledgeSupertag.id == KnowledgeNodeSupertag.supertag_id)
                .where(KnowledgeNodeSupertag.node_id.in_(batch), KnowledgeSupertag.system_key == 'project_info'))
            tagged.update(result.all())
        for batch in _acl_batches(hubs):
            result = await self.session.execute(select(KnowledgeNode.parent_id, KnowledgeNode.project_id, KnowledgeNode.docs_library_id)
                .where(KnowledgeNode.parent_id.in_(batch), KnowledgeNode.project_id.is_not(None), KnowledgeNode.archived_at.is_(None)))
            for row in result:
                if row.docs_library_id == nodes[row.parent_id].docs_library_id:
                    children.setdefault(row.parent_id, set()).add(row.project_id)
        revisions = {}
        for batch in _acl_batches(ids['generation_output']):
            result = await self.session.execute(select(MediaAssetRevision.generation_output_id, MediaAssetRevision.owner_user_id,
                                                       MediaAssetRevision.project_id).where(MediaAssetRevision.generation_output_id.in_(batch)))
            revisions.update((r.generation_output_id, SimpleNamespace(**r._mapping)) for r in result)
        project_ids = {r.project_id for collection in [conversations, tasks, nodes, rows['generation_output'], revisions]
                       for r in collection.values() if r.project_id is not None}
        project_ids.update(project for values in children.values() for project in values)
        project_ids.update(r.owner_id for r in rows['workspace_file'].values()
                           if r.scope == 'project' and r.owner_id is not None)
        knowledge_refs, meeting_projects = {}, {}
        for r in rows['knowledge_document'].values():
            try:
                knowledge_refs[r.id] = [uuid.UUID(str(value)) for value in (r.project_refs or [])]
                project_ids.update(knowledge_refs[r.id])
            except (ValueError, TypeError, AttributeError):
                continue
        for r in rows['meeting_audio'].values():
            try:
                request = decrypt_json_value_if_needed(r.request_json, aad='meeting_processing_jobs.request_json') or {}
                project = request.get('project_id')
                meeting_projects[r.id] = uuid.UUID(str(project)) if project is not None else None
                if project is not None:
                    project_ids.add(meeting_projects[r.id])
            except (ValueError, TypeError, AttributeError):
                continue
        projects = await self._acl_projects(project_ids, actor)
        readable_sources = await self._acl_knowledge_sources({r.source_id for r in rows['knowledge_document'].values()}, actor)
        shared = await self._acl_doc_shares({r.id for r in nodes.values() if r.project_id is None
            and r.docs_library_id in libraries and libraries[r.docs_library_id].owner_user_id != actor.id
            and not str(r.system_key or '').strip().startswith('project_information:')}, actor)

        def scoped(project):
            if project is None:
                return self._scope_allows_project(None)
            return project in projects and projects[project].scoped_read

        def conversation(identifier):
            row = conversations.get(identifier)
            return bool(row and row.deleted_at is None and scoped(row.project_id)
                        and self._scope_allows_character(row.character_name)
                        and (str(row.user_id) == str(actor.id) or row.id in joined))

        def operations_access(project, owner):
            if project is None:
                return owner == actor.id or str(actor.role or '').strip().lower() == 'admin'
            return project in projects and projects[project].operations_read

        def docs_access(node):
            library = libraries.get(node.docs_library_id)
            if library is None or node.archived_at is not None or not scoped(node.project_id):
                return False
            owner = library.owner_user_id == actor.id
            system = str(node.system_key or '').strip()
            if system == 'project_information_root' and node.project_id is not None:
                return owner
            if system.startswith('project_information:'):
                if owner:
                    return True
                project, hub = projects.get(node.project_id), parents.get(node.parent_id)
                return bool(project and project.read and system == f'project_information:{node.project_id}'
                    and pointers.get(node.id) == {node.project_id} and not project.is_completed
                    and library.library_type == 'personal' and library.owner_user_id == project.owner_id
                    and node.node_type == 'node' and node.is_explicit_blank is not True
                    and node.parent_id is not None and node.root_page_id == node.parent_id
                    and hub and hub.docs_library_id == node.docs_library_id
                    and str(hub.system_key or '').strip() == 'project_information_root'
                    and hub.parent_id is None and hub.root_page_id in (None, hub.id)
                    and hub.archived_at is None and str(hub.title or '').strip() == '案件情報'
                    and (node.id, node.docs_library_id) in tagged)
            if node.project_id is not None:
                return projects[node.project_id].read
            if system == 'project_information_root':
                if not (library.library_type == 'personal' and node.parent_id is None
                        and node.root_page_id in (None, node.id) and str(node.title or '').strip() == '案件情報'):
                    return False
                if any(projects.get(p) and projects[p].read for p in children.get(node.id, ())):
                    return True
            return owner or node.id in shared

        allowed = []
        for key, (kind, identifier, index) in parsed.items():
            r = rows[kind].get(identifier)
            if r is None:
                continue
            permitted = False
            if kind == 'generation_output':
                revision = revisions.get(identifier)
                permitted = bool(revision and scoped(r.project_id)
                    and operations_access(r.project_id, r.owner_user_id)
                    and operations_access(revision.project_id, revision.owner_user_id))
            elif kind == 'generated_media':
                if r.status == 'succeeded':
                    if r.context_type == 'conversation':
                        session_id = _acl_uuid(r.context_id)
                        session = conversations.get(session_id)
                        permitted = bool(conversation(session_id) and session
                            and (str(r.owner_user_id) == str(actor.id) or str(session.user_id) == str(actor.id)))
                    else:
                        permitted = str(r.owner_user_id) == str(actor.id) and scoped(None)
            elif kind in {'chat_attachment', 'conversation_message'}:
                permitted = (r.deleted_at is None and r.is_active_branch is not False
                             and not is_privacy_masking_source(r.message_metadata) and conversation(r.session_id))
                if permitted and kind == 'chat_attachment':
                    try:
                        item = (r.message_metadata or {}).get('attachments', [])[index]
                        session = conversations[r.session_id]
                        namespace = (f'_projects/project_{session.project_id}' if session.project_id
                                     else f'_users/user_{session.user_id}')
                        permitted = str(item.get('path') or '').replace('\\', '/').startswith(namespace + '/')
                    except (ValueError, TypeError, IndexError, AttributeError, KeyError):
                        permitted = False
            elif kind == 'task_attachment':
                task = tasks.get(r.task_id)
                permitted = bool(task and task.deleted_at is None and task.project_id is not None
                                 and task.project_id == r.project_id and scoped(task.project_id))
            elif kind == 'docs_attachment':
                node = nodes.get(r.node_id)
                permitted = bool(node and docs_access(node))
            elif kind == 'knowledge_document':
                permitted = r.status == 'active' and r.source_id in readable_sources and r.id in knowledge_refs
                if permitted and require_local_original:
                    permitted = readable_sources[r.source_id] != 'growi'
                if permitted and getattr(self.scope, 'project_scope_restricted', False):
                    if self.scope.project_id is None:
                        permitted = not knowledge_refs[r.id]
                    else:
                        permitted = self.scope.project_id in knowledge_refs[r.id] and scoped(self.scope.project_id)
                elif permitted and self.scope.project_id is not None:
                    permitted = self.scope.project_id in knowledge_refs[r.id] and scoped(self.scope.project_id)
                elif permitted and self.scope.space_id is not None:
                    permitted = any(scoped(p) for p in knowledge_refs[r.id])
            elif kind == 'meeting_audio':
                permitted = (r.actor_user_id == actor.id and r.status != 'succeeded'
                             and r.id in meeting_projects and scoped(meeting_projects[r.id]))
            elif kind == 'workspace_file':
                # User Files: only the owner, even for admins. Project Files:
                # the live canonical Project read policy and search scope.
                if r.status == 'indexable' and r.deleted_at is None:
                    if r.scope == 'user':
                        permitted = str(r.owner_id) == str(actor.id) and scoped(None)
                    elif r.scope == 'project':
                        permitted = scoped(r.owner_id)
            if permitted:
                allowed.append(key)
        return allowed

    async def _project(self, project_id):
        actor = await self._actor()
        if project_id is None:
            if not self._scope_allows_project(None):
                raise SourceUnavailable("Source unavailable in this scope")
            return None
        project_id = uuid.UUID(str(project_id))
        if (getattr(self.scope, 'project_scope_restricted', False)
                and project_id != self.scope.project_id):
            raise SourceUnavailable("Source unavailable in this scope")
        if self.scope.project_id is not None and project_id != self.scope.project_id:
            raise SourceUnavailable("Source unavailable in this scope")
        project = await self.session.get(Project, project_id, populate_existing=True)
        if project is None or project.deleted_at is not None:
            raise SourceUnavailable("Source unavailable")
        if self.scope.space_id is not None and project.space_id != self.scope.space_id:
            raise SourceUnavailable("Source unavailable in this Space")
        if not await ProjectRepository.has_permission(
            self.session, project_id, self.scope.user_id, "read"
        ):
            raise SourceUnavailable("Source unavailable")
        if project.space_id is not None:
            from .space_access import get_readable_space

            # The canonical Space policy selects an ORM entity. Refresh its
            # identity-map entry before it evaluates owner/Inbox state.
            if await self.session.get(Space, project.space_id, populate_existing=True) is None:
                raise SourceUnavailable("Source unavailable in this Space")
            if (
                await get_readable_space(
                    self.session,
                    space_id=project.space_id,
                    user_id=self.scope.user_id,
                    user_info={"role": actor.role},
                )
                is None
            ):
                raise SourceUnavailable("Source unavailable in this Space")
        return project

    async def _actor(self):
        actor = await self.session.get(User, self.scope.user_id, populate_existing=True)
        if actor is None or actor.is_active is not True:
            raise SourceUnavailable("Authenticated source user unavailable")
        return actor

    async def privacy_masked(self, ref):
        """Internal metadata-only authority for withdrawing forbidden derivatives.

        This never authorizes original access or returns metadata to a caller.
        Source resolution retains its independent current actor/ACL checks.
        """
        if ref.kind not in {"chat_attachment", "conversation_message"}:
            return False
        identifier = _acl_uuid(ref.id.split(":", 1)[0])
        if identifier is None:
            return False
        metadata = await self.session.scalar(select(ConversationMessage.message_metadata).where(
            ConversationMessage.id == identifier))
        return is_privacy_masking_source(metadata)

    @staticmethod
    def _file(root: Path, relative: str) -> Path:
        root = lexical_path(root)
        try:
            assert_no_links(root)
            info = root.stat()
            if not stat.S_ISDIR(info.st_mode):
                raise OSError("original storage root is not a directory")
            # A root outage is not evidence that the registered child was
            # deleted.  Keep it retryable so an old ready point survives a
            # transient/unmounted storage failure.
            with os.scandir(root):
                pass
        except StorageError as exc:
            raise SourceUnavailable(reason="original_path_invalid") from exc
        except (OSError, RuntimeError) as exc:
            raise SourceUnavailable(reason="original_storage_failure") from exc
        try:
            parts = relative_parts(relative)
        except StorageError as exc:
            raise SourceUnavailable(reason="original_path_invalid") from exc
        target = root.joinpath(*parts)
        try:
            assert_no_links(target)
            info = target.stat()
        except FileNotFoundError as exc:
            raise SourceUnavailable(reason="original_missing") from exc
        except StorageError as exc:
            raise SourceUnavailable(reason="original_path_invalid") from exc
        except (OSError, RuntimeError) as exc:
            raise SourceUnavailable(reason="original_storage_failure") from exc
        if not stat.S_ISREG(info.st_mode):
            raise SourceUnavailable(reason="original_path_invalid")
        return target

    async def _conversation(self, session_id):
        row = await self.session.get(
            ConversationSession, uuid.UUID(str(session_id)), populate_existing=True
        )
        if row is None or row.deleted_at is not None:
            raise SourceUnavailable("Conversation unavailable")
        await self._project(row.project_id)
        if not self._scope_allows_character(getattr(row, 'character_name', None)):
            raise SourceUnavailable("Conversation unavailable")
        if str(row.user_id) != str(self.scope.user_id):
            participant = (
                await self.session.execute(
                    select(ConversationParticipant.id)
                    .where(
                        ConversationParticipant.session_id == row.id,
                        ConversationParticipant.participant_type == "user",
                        ConversationParticipant.participant_id
                        == str(self.scope.user_id),
                        ConversationParticipant.status == "joined",
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if participant is None:
                raise SourceUnavailable("Conversation unavailable")
        return row

    async def resolve(
        self, ref: SourceRef, *, open_file: bool = True
    ) -> ResolvedSource:
        """Recheck current permissions; ``open_file=False`` still checks row ACL."""
        try:
            return await self._resolve(ref, open_file=open_file)
        except SourceUnavailable:
            raise
        except (
            ValueError,
            TypeError,
            IndexError,
            KeyError,
            FileNotFoundError,
            OSError,
        ) as exc:
            raise SourceUnavailable("Source unavailable") from exc

    async def _resolve(self, ref, *, open_file):
        await self._actor()
        path, text, segments = None, "", []
        resource = None
        meta: dict[str, Any] = {"source_id": ref.key, "source_kind": ref.kind}
        if ref.kind == "generation_output":
            from .media_asset_service import MediaAssetService
            from .media_operations_service import MediaOperationsError

            output = await self.session.get(
                GenerationOutput, uuid.UUID(ref.id), populate_existing=True
            )
            if output is None:
                raise SourceUnavailable()
            await self._project(output.project_id)
            service = MediaAssetService(workspace_root=self.workspace_root)
            actor = await self._actor()
            try:
                await service._assert_entity_access(
                    self.session, actor, output, permission="read"
                )
                revision = (
                    await self.session.execute(
                        select(MediaAssetRevision)
                        .where(MediaAssetRevision.generation_output_id == output.id)
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                if revision is None:
                    raise SourceUnavailable(
                        reason="generation_output_not_registered_as_managed_asset"
                    )
                if open_file:
                    # Copy through the descriptor-pinned reader. Reopening its
                    # original pathname would lose replacement protection.
                    scratch = Path(__file__).resolve().parents[2] / ".local"
                    scratch.mkdir(parents=True, exist_ok=True)
                    resource = tempfile.TemporaryDirectory(
                        prefix="eg2-source-", dir=scratch
                    )
                    target = Path(resource.name) / "payload"
                    try:
                        materialized = await service.materialize_for_provider(
                            self.session,
                            output.id,
                            owner_user_id=revision.owner_user_id,
                            project_id=revision.project_id,
                            expected_sha256=revision.sha256,
                            target=target,
                        )
                        if materialized is None:
                            raise SourceUnavailable(reason="managed_asset_unavailable")
                        await service.get_asset(
                            self.session, await self._actor(), output.id
                        )
                        await self._project(output.project_id)
                        path = materialized.path
                        verified = revision
                    except BaseException:
                        resource.cleanup()
                        raise
                else:
                    await service.get_asset(self.session, actor, output.id)
                    verified = revision
            except MediaOperationsError as exc:
                raise SourceUnavailable(reason="managed_asset_unavailable") from exc
            title = f"Output {output.external_asset_id}"
            text = (
                f"generation_output_id: {output.id}\nasset_revision_id: {verified.id}"
            )
            meta.update(
                project_id=str(output.project_id) if output.project_id else None,
                asset_revision_id=str(verified.id),
                mime_type=verified.mime_type,
                original_url=f"/api/operations/media/assets/{output.id}/preview?expected_sha256={verified.sha256}",
            )
        elif ref.kind == "generated_media":
            from .generated_media_service import (
                user_can_access_media,
                resolve_media_file,
            )

            row = await self.session.get(
                GeneratedMedia, uuid.UUID(ref.id), populate_existing=True
            )
            if (
                row is None
                or row.status != "succeeded"
                or not await user_can_access_media(str(self.scope.user_id), row, session=self.session)
            ):
                raise SourceUnavailable("Source unavailable")
            conversation = (
                await self._conversation(row.context_id)
                if row.context_type == "conversation"
                else None
            )
            if conversation is None:
                await self._project(None)
            meta.update(
                session_id=row.context_id if conversation else None,
                message_id=row.bind_id,
                original_url=f"/api/generated-media/{row.id}",
                mime_type=row.mime_type,
                project_id=str(conversation.project_id)
                if conversation and conversation.project_id
                else None,
            )
            title = Path(row.relative_path).name
            text = str((row.prompt_meta or {}).get("prompt") or "")
            path = resolve_media_file(row, create_root=False) if open_file else None
            if open_file and path is None:
                raise SourceUnavailable(reason="original_missing")
        elif ref.kind in {"chat_attachment", "conversation_message"}:
            parts = ref.id.split(":")
            row = await self.session.get(
                ConversationMessage, uuid.UUID(parts[0]), populate_existing=True
            )
            if (
                row is None
                or row.deleted_at is not None
                or row.is_active_branch is False
            ):
                raise SourceUnavailable("Source unavailable")
            conversation = await self._conversation(row.session_id)
            if is_privacy_masking_source(row):
                raise SourceUnavailable("privacy_masking_source", reason="privacy_masking_source")
            meta.update(
                session_id=str(row.session_id),
                message_id=str(row.id),
                role=row.role,
                timestamp=row.created_at.isoformat() if row.created_at else None,
                project_id=str(conversation.project_id)
                if conversation.project_id
                else None,
                original_url=f"/chat?session={row.session_id}&message={row.id}",
            )
            title, text = conversation.title or "Conversation", row.content
            if ref.kind == "chat_attachment":
                index = int(parts[1])
                if index < 0:
                    raise SourceUnavailable("Source unavailable")
                item = (row.message_metadata or {}).get("attachments", [])[index]
                relative = str(item.get("path") or "").replace("\\", "/")
                namespace = (
                    (await ProjectRepository.get_storage_path(conversation.project_id))
                    if conversation.project_id
                    else f"_users/user_{conversation.user_id}"
                )
                if not relative.startswith(namespace.rstrip("/") + "/"):
                    raise SourceUnavailable(reason="original_path_invalid")
                title = str(
                    item.get("name") or item.get("filename") or Path(relative).name
                )
                text = str(item.get("text") or item.get("extracted_text") or "")
                meta["mime_type"] = item.get("mime_type")
                path = self._file(self.workspace_root, relative) if open_file else None
        elif ref.kind == "task_attachment":
            row = await self.session.get(
                TaskAttachment, uuid.UUID(ref.id), populate_existing=True
            )
            task = (
                await self.session.get(Task, row.task_id, populate_existing=True)
                if row
                else None
            )
            if (
                task is None
                or task.deleted_at is not None
                or task.project_id != row.project_id
            ):
                raise SourceUnavailable("Source unavailable")
            project = await self._project(task.project_id)
            root = self.workspace_root.joinpath(
                *relative_parts(await ProjectRepository.get_storage_path(project.id))
            )
            title, text = (
                row.display_name,
                str((row.attachment_metadata or {}).get("extracted_text") or ""),
            )
            meta.update(
                project_id=str(project.id),
                task_id=str(task.id),
                mime_type=row.mime_type,
                original_url=f"/api/tasks/{task.id}/attachments/{row.id}",
            )
            path = self._file(root, row.file_path) if open_file else None
        elif ref.kind == "docs_attachment":
            from .docs_acl import can_read_node
            from .clip_ingest_storage import ClipIngestStorage, ClipUploadError

            # Library ownership, identity hubs and ancestor edges can remain
            # cached by a long-lived ORM session. Fresh SQL is the final gate.
            if ref.key not in await self.allowed_refs([ref]):
                raise SourceUnavailable()
            row = await self.session.get(
                KnowledgeAttachment, uuid.UUID(ref.id), populate_existing=True
            )
            node = (
                await self.session.get(
                    KnowledgeNode, row.node_id, populate_existing=True
                )
                if row
                else None
            )
            if (
                node is None
                or node.archived_at is not None
                or not await can_read_node(self.session, node, self.scope.user_id)
            ):
                raise SourceUnavailable("Source unavailable")
            await self._project(node.project_id)
            title = row.file_name
            details = row.attachment_metadata or {}
            text = "\n".join(
                str(details.get(key) or "")
                for key in (
                    "extracted_text",
                    "ocr_text",
                    "recognition",
                    "alt_text",
                    "caption",
                )
            )
            meta.update(
                node_id=str(node.id),
                docs_library_id=str(node.docs_library_id),
                project_id=str(node.project_id) if node.project_id else None,
                original_url=f"/api/docs/attachments/{row.id}",
                mime_type=row.mime_type,
            )
            path = None
            if open_file:
                try:
                    path = ClipIngestStorage(
                        self.workspace_root, create_root=False
                    ).resolve_attachment_path(row.file_path)
                except ClipUploadError as exc:
                    raise SourceUnavailable(reason=exc.reason) from exc
        elif ref.kind == "knowledge_document":
            from ..knowledge.service import KnowledgeService

            # Remote GROWI may be authorized, but has no native local original.
            # Keep the explicit exclusion below rather than masking it as ACL.
            if ref.key not in await self.allowed_refs([ref], require_local_original=False):
                raise SourceUnavailable()
            row = await self.session.get(
                KnowledgeDocument, uuid.UUID(ref.id), populate_existing=True
            )
            source = (
                await self.session.get(
                    KnowledgeSource, row.source_id, populate_existing=True
                )
                if row
                else None
            )
            if (
                source is None
                or row.status != "active"
                or not await KnowledgeService.can_read_source(
                    self.session,
                    source_id=source.id,
                    actor_user_id=self.scope.user_id,
                )
            ):
                raise SourceUnavailable("Source unavailable")
            refs = [uuid.UUID(str(x)) for x in (row.project_refs or [])]
            if getattr(self.scope, 'project_scope_restricted', False):
                if self.scope.project_id is None:
                    if refs:
                        raise SourceUnavailable("Source unavailable in this Project")
                elif self.scope.project_id not in refs:
                    raise SourceUnavailable("Source unavailable in this Project")
            elif self.scope.project_id is not None and self.scope.project_id not in refs:
                raise SourceUnavailable("Source unavailable in this Project")
            if self.scope.project_id is not None:
                await self._project(self.scope.project_id)
            elif self.scope.space_id is not None:
                permitted = False
                for project_id in refs:
                    try:
                        await self._project(project_id)
                        permitted = True
                        break
                    except SourceUnavailable:
                        continue
                if not permitted:
                    raise SourceUnavailable("Source unavailable in this Space")
            title = row.title or Path(row.path).name
            meta.update(
                document_id=str(row.id),
                knowledge_source_id=str(source.id),
                project_refs=[str(x) for x in refs],
                original_url=f"/api/knowledge/documents/{row.id}",
            )
            if source.source_type == "growi":
                raise SourceUnavailable(
                    reason="remote_growi_has_no_local_media_original"
                )
            path = (
                self._file(lexical_path(source.root_path), row.path)
                if open_file
                else None
            )
        elif ref.kind == "workspace_file":
            from .workspace_file_index import files_location, index_format

            row = await self.session.get(
                WorkspaceFileEntry, uuid.UUID(ref.id), populate_existing=True
            )
            if row is None or row.deleted_at is not None:
                raise SourceUnavailable(reason="canonical_source_deleted")
            if row.status != "indexable" or not row.content_hash:
                raise SourceUnavailable(reason="workspace_file_not_indexable")
            if row.scope == "user":
                if str(row.owner_id) != str(self.scope.user_id):
                    raise SourceUnavailable("Source unavailable")
                await self._project(None)
                root = self.workspace_root.joinpath("_users", f"user_{row.owner_id}")
            elif row.scope == "project":
                project = await self._project(row.owner_id)
                root = self.workspace_root.joinpath(
                    *relative_parts(await ProjectRepository.get_storage_path(project.id))
                )
                meta["project_id"] = str(project.id)
            else:
                raise SourceUnavailable("Source unavailable")
            title = row.rel_path
            location = files_location(row.scope, row.owner_id, row.rel_path)
            meta.update(
                mime_type=row.mime,
                content_hash=row.content_hash,
                index_format=index_format(row.name),
                files_location=location,
                files_scope=row.scope,
                files_owner_id=str(row.owner_id),
                original_url="/api/explorer/serve?path=" + quote(location["path"], safe="/"),
            )
            if open_file:
                path = self._file(root, row.rel_path)
                info = path.stat()
                # A changed original waits for the scanner's rehash; serving
                # it now would mix an old verified hash with new bytes.
                if (int(info.st_size), int(info.st_mtime_ns)) != (int(row.size_bytes), int(row.mtime_ns)):
                    raise SourceUnavailable(reason="original_changed")
        elif ref.kind == "meeting_audio":
            from .meeting_processing_storage import MeetingAudioStorage

            row = await self.session.get(
                MeetingProcessingJob, uuid.UUID(ref.id), populate_existing=True
            )
            if row is None or row.actor_user_id != self.scope.user_id:
                raise SourceUnavailable("Source unavailable")
            if row.status == "succeeded":
                raise SourceUnavailable(reason="meeting_audio_discarded_after_success")
            request = row.request_json or {}
            project_id = request.get("project_id")
            await self._project(project_id)
            title = row.audio_file_name
            result = row.result_json or {}
            transcript = result.get("transcript") or {}
            text = (
                str(transcript.get("text") or "")
                if isinstance(transcript, dict)
                else str(transcript)
            )
            segments = (
                transcript.get("segments") or [] if isinstance(transcript, dict) else []
            )
            meta.update(
                meeting_job_id=str(row.id),
                project_id=project_id,
                original_url=f"/api/v1/meeting-processing/jobs/{row.id}",
                mime_type=row.audio_mime_type,
            )
            if open_file:
                from .clip_ingest_storage import ClipUploadError

                try:
                    upload = MeetingAudioStorage(self.workspace_root, create_root=False).resolve_upload(
                        row.actor_user_id, row.audio_upload_id
                    )
                except (ClipUploadError, FileNotFoundError) as exc:
                    reason = getattr(exc, "reason", "source_unavailable")
                    if reason not in {
                        "original_missing",
                        "original_path_invalid",
                        "original_storage_failure",
                    }:
                        # The legacy meeting reason is retained for callers,
                        # but media-index cleanup treats it as retryable.  A
                        # typed storage gap is the only terminal original
                        # failure here.
                        reason = "meeting_original_missing_or_invalid"
                    raise SourceUnavailable(
                        reason=reason
                    ) from exc
                if (
                    upload.sha256 != row.audio_sha256
                    or upload.size_bytes != row.audio_size_bytes
                    or upload.file_name != row.audio_file_name
                ):
                    raise SourceUnavailable("Original meeting audio integrity changed")
                path = upload.payload_path
        else:
            raise SourceUnavailable("unsupported_source_kind")
        if open_file and ref.kind != "conversation_message" and path is None:
            raise SourceUnavailable("Original file unavailable")
        return ResolvedSource(ref, path, title, text, meta, segments, resource)

    async def inventory(self, *, kinds=None) -> AsyncIterator[SourceRef]:
        """Bounded keyset enumeration; resolve each row before indexing/querying."""
        models = {
            "generation_output": GenerationOutput,
            "generated_media": GeneratedMedia,
            "task_attachment": TaskAttachment,
            "docs_attachment": KnowledgeAttachment,
            "knowledge_document": KnowledgeDocument,
            "meeting_audio": MeetingProcessingJob,
            "chat_attachment": ConversationMessage,
            "workspace_file": WorkspaceFileEntry,
        }
        for kind, model in models.items():
            if kinds is not None and kind not in kinds:
                continue
            cursor = None
            while True:
                stmt = select(model).order_by(model.id).limit(100)
                if cursor is not None:
                    stmt = stmt.where(model.id > cursor)
                rows = list((await self.session.execute(stmt)).scalars().all())
                if not rows:
                    break
                for row in rows:
                    if kind == "chat_attachment":
                        for index, _ in enumerate(
                            (row.message_metadata or {}).get("attachments") or []
                        ):
                            yield SourceRef(kind, f"{row.id}:{index}")
                    else:
                        yield SourceRef(kind, str(row.id))
                cursor = rows[-1].id
