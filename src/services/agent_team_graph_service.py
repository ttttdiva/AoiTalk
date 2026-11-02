"""Persistence and authorization for external Agent Team graph drafts."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ..memory.database import get_database_manager
from ..memory.models import Agent, AgentRevision, Project, ProjectMember
from ..memory.models.agent_team_graph import AgentTeamGraph, AgentTeamGraphRevision
from ..security.field_crypto import decrypt_text, encrypt_text
from .agent_identity_service import AgentIdentityService
from .agent_team_graph_schema import graph_diagnostics, normalize_graph
from .project_permissions import has_effective_project_permission


class AgentTeamGraphError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _uuid(value: Any, label: str, *, optional: bool = False) -> UUID | None:
    if value is None and optional:
        return None
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise AgentTeamGraphError(f"invalid {label}") from exc


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _envelope(title, project_id, definition):
    return {"title": title, "project_id": str(project_id) if project_id else None, "definition": definition}


def _title(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 160:
        raise AgentTeamGraphError("invalid title")
    return value.strip()


def _definition(value: Any) -> dict:
    try:
        return normalize_graph(value)
    except (ValidationError, ValueError, TypeError) as exc:
        raise AgentTeamGraphError("invalid graph definition") from exc


def _aad(revision_id: UUID) -> str:
    return f"agent_team_graph_revision:{revision_id}:definition"


class AgentTeamGraphService:
    def __init__(self, db_manager: Any | None = None):
        self._db_manager = db_manager
        self.identity = AgentIdentityService(db_manager)

    async def _session(self):
        return await (self._db_manager or get_database_manager()).get_session()

    async def _actor(self, session, actor_user_id):
        return await self.identity._assert_admin(session, actor_user_id)

    async def _project(self, session, project_id, actor, permission):
        if project_id is None:
            return
        project = await session.get(Project, project_id)
        if project is None or project.deleted_at is not None:
            raise AgentTeamGraphError("project not found", 404)
        member = await session.scalar(select(ProjectMember).where(
            ProjectMember.project_id == project_id, ProjectMember.user_id == actor
        ))
        if not has_effective_project_permission(
            user_id=actor, user_role="admin", project_owner_id=project.owner_id,
            member_permissions=member.permissions if member else None, permission=permission,
        ):
            raise AgentTeamGraphError("project access denied", 403)

    async def _graph(self, session, graph_id, actor, permission="read", *, for_update=False):
        graph = await session.get(AgentTeamGraph, _uuid(graph_id, "graph_id"), with_for_update=for_update)
        if graph is None or graph.created_by != actor:
            raise AgentTeamGraphError("graph not found", 404)
        await self._project(session, graph.project_id, actor, permission)
        return graph

    async def _references(self, session, definition, actor):
        for node in definition["nodes"]:
            agent = await session.get(Agent, _uuid(node["agent_id"], "agent_id"))
            if agent is None or agent.created_by != actor:
                raise AgentTeamGraphError("agent not found", 404)
            if node["revision_id"] is not None:
                revision = await session.get(AgentRevision, _uuid(node["revision_id"], "revision_id"))
                if revision is None or revision.agent_id != agent.id:
                    raise AgentTeamGraphError("agent revision not found", 404)

    async def _projection(self, session, graph, revision, actor, *, live_diagnostics=True):
        await self._project(session, revision.project_id, actor, "read")
        try:
            raw = decrypt_text(revision.definition_encrypted, aad=_aad(revision.id))
            definition = _definition(json.loads(raw))
        except (ValueError, TypeError) as exc:
            raise AgentTeamGraphError("invalid stored graph definition", 409) from exc
        if _hash(_envelope(revision.title, revision.project_id, definition)) != revision.content_hash:
            raise AgentTeamGraphError("graph content hash mismatch", 409)
        diagnostics = graph_diagnostics(definition)
        # State is live readiness information; never replace a pinned revision.
        if live_diagnostics:
            for node in definition["nodes"]:
                agent = await session.get(Agent, _uuid(node["agent_id"], "agent_id"))
                if agent is None or agent.state != "active":
                    diagnostics.append(f"agent_not_active:{node['key']}")
        return {
            "id": str(graph.id), "title": revision.title,
            "project_id": str(revision.project_id) if revision.project_id else None,
            "created_by": str(graph.created_by), "created_at": graph.created_at.isoformat(),
            "updated_at": graph.updated_at.isoformat(),
            "archived_at": graph.archived_at.isoformat() if graph.archived_at else None,
            "current_revision_id": str(graph.current_revision_id),
            "revision_id": str(revision.id), "version": revision.version,
            "content_hash": revision.content_hash, "definition": definition,
            "diagnostics": diagnostics,
        }

    @staticmethod
    def _new_revision(graph_id, version, title, project_id, definition, actor):
        rid = uuid4()
        return AgentTeamGraphRevision(
            id=rid, graph_id=graph_id, version=version, title=title, project_id=project_id,
            content_hash=_hash(_envelope(title, project_id, definition)),
            definition_encrypted=encrypt_text(_canonical(definition), aad=_aad(rid)), created_by=actor,
        )

    async def create(self, *, actor_user_id, title, project_id=None, idempotency_key, definition):
        title, project_id, definition = _title(title), _uuid(project_id, "project_id", optional=True), _definition(definition)
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 255:
            raise AgentTeamGraphError("invalid idempotency key")
        digest = _hash(_envelope(title, project_id, definition))
        session = await self._session()
        try:
            actor = await self._actor(session, actor_user_id)
            await self._project(session, project_id, actor, "write")
            existing = await session.scalar(select(AgentTeamGraph).where(AgentTeamGraph.created_by == actor, AgentTeamGraph.idempotency_key == idempotency_key))
            if existing:
                if existing.create_hash != digest:
                    raise AgentTeamGraphError("idempotency conflict", 409)
                await self._project(session, existing.project_id, actor, "read")
                revision = await session.scalar(select(AgentTeamGraphRevision).where(
                    AgentTeamGraphRevision.graph_id == existing.id, AgentTeamGraphRevision.version == 1))
                return await self._projection(session, existing, revision, actor)
            await self._references(session, definition, actor)
            graph = AgentTeamGraph(id=uuid4(), title=title, project_id=project_id, created_by=actor,
                                   idempotency_key=idempotency_key, create_hash=digest)
            session.add(graph)
            await session.flush()
            revision = self._new_revision(graph.id, 1, title, project_id, definition, actor)
            session.add(revision)
            await session.flush()
            graph.current_revision_id = revision.id
            await session.commit()
            return await self._projection(session, graph, revision, actor)
        except IntegrityError as exc:
            await session.rollback()
            # A concurrent winner is handled through the same idempotency path.
            existing = await session.scalar(select(AgentTeamGraph).where(AgentTeamGraph.created_by == actor, AgentTeamGraph.idempotency_key == idempotency_key))
            if existing and existing.created_by == actor and existing.create_hash == digest:
                await self._project(session, existing.project_id, actor, "read")
                revision = await session.scalar(select(AgentTeamGraphRevision).where(
                    AgentTeamGraphRevision.graph_id == existing.id, AgentTeamGraphRevision.version == 1))
                return await self._projection(session, existing, revision, actor)
            raise AgentTeamGraphError("graph creation conflict", 409) from exc
        finally:
            await session.close()

    async def update(self, graph_id, *, actor_user_id, expected_revision, title, project_id=None, definition):
        title, project_id, definition = _title(title), _uuid(project_id, "project_id", optional=True), _definition(definition)
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
            raise AgentTeamGraphError("invalid expected revision")
        session = await self._session()
        try:
            actor = await self._actor(session, actor_user_id)
            graph = await self._graph(session, graph_id, actor, "write")
            await self._project(session, project_id, actor, "write")
            current = await session.get(AgentTeamGraphRevision, graph.current_revision_id)
            if current is None or current.version != expected_revision:
                raise AgentTeamGraphError("stale revision", 409)
            await self._references(session, definition, actor)
            revision = self._new_revision(graph.id, current.version + 1, title, project_id, definition, actor)
            session.add(revision)
            await session.flush()
            result = await session.execute(update(AgentTeamGraph).where(
                AgentTeamGraph.id == graph.id, AgentTeamGraph.current_revision_id == current.id
            ).values(title=title, project_id=project_id, current_revision_id=revision.id, updated_at=datetime.utcnow()))
            if result.rowcount != 1:
                raise AgentTeamGraphError("stale revision", 409)
            await session.commit()
            await session.refresh(graph)
            return await self._projection(session, graph, revision, actor)
        except IntegrityError as exc:
            await session.rollback()
            raise AgentTeamGraphError("stale revision", 409) from exc
        finally:
            await session.close()

    async def get_in_session(self, session, graph_id, *, actor_user_id, revision_id):
        """Read an exact authorized historical snapshot in the caller's transaction."""
        actor = await self._actor(session, actor_user_id)
        graph = await self._graph(session, graph_id, actor)
        revision = await session.get(AgentTeamGraphRevision, _uuid(revision_id, "revision_id"))
        if revision is None or revision.graph_id != graph.id:
            raise AgentTeamGraphError("revision not found", 404)
        return await self._projection(session, graph, revision, actor, live_diagnostics=False)

    async def get(self, graph_id, *, actor_user_id, revision_id=None):
        session = await self._session()
        try:
            actor = await self._actor(session, actor_user_id)
            graph = await self._graph(session, graph_id, actor)
            rid = _uuid(revision_id, "revision_id", optional=True) or graph.current_revision_id
            revision = await session.get(AgentTeamGraphRevision, rid)
            if revision is None or revision.graph_id != graph.id:
                raise AgentTeamGraphError("revision not found", 404)
            return await self._projection(session, graph, revision, actor)
        finally:
            await session.close()

    async def revisions(self, graph_id, *, actor_user_id):
        session = await self._session()
        try:
            actor = await self._actor(session, actor_user_id)
            graph = await self._graph(session, graph_id, actor)
            rows = (await session.scalars(select(AgentTeamGraphRevision).where(
                AgentTeamGraphRevision.graph_id == graph.id).order_by(AgentTeamGraphRevision.version.desc()))).all()
            return [await self._projection(session, graph, row, actor) for row in rows]
        finally:
            await session.close()

    async def set_archived(self, graph_id, *, actor_user_id, archived):
        session = await self._session()
        try:
            actor = await self._actor(session, actor_user_id)
            graph = await self._graph(session, graph_id, actor, "write", for_update=True)
            if (graph.archived_at is not None) != archived:
                graph.archived_at = datetime.utcnow() if archived else None
            revision = await session.get(AgentTeamGraphRevision, graph.current_revision_id)
            projection = await self._projection(session, graph, revision, actor)
            await session.commit()
            return projection
        finally:
            await session.close()

    async def list(self, *, actor_user_id, include_archived=False):
        session = await self._session()
        try:
            actor = await self._actor(session, actor_user_id)
            query = select(AgentTeamGraph).where(AgentTeamGraph.created_by == actor)
            if not include_archived:
                query = query.where(AgentTeamGraph.archived_at.is_(None))
            rows = (await session.scalars(query.order_by(AgentTeamGraph.updated_at.desc()))).all()
            result = []
            for graph in rows:
                try:
                    await self._project(session, graph.project_id, actor, "read")
                except AgentTeamGraphError:
                    continue
                revision = await session.get(AgentTeamGraphRevision, graph.current_revision_id)
                result.append(await self._projection(session, graph, revision, actor))
            return result
        finally:
            await session.close()
