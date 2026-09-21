"""Canonical Docs persistence for durable meeting-processing jobs.

Meeting artifacts intentionally bypass the semantic ClipIngest pipeline.  The
job UUID is the identity of each artifact, which makes retries and process
restarts safe even when a process dies after the Docs transaction commits.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.docs_sync import apply_docs_operation
from ..memory.models import DocsLibrary, KnowledgeNode, KnowledgeNodeSupertag, KnowledgeSupertag
from ..memory.models.meeting_processing import MeetingProcessingJob
from .docs_graph_service import DocsGraphService
from .docs_workspace import ensure_docs_library

ArtifactType = Literal["minutes", "memo"]


class MeetingDocsError(RuntimeError):
    """Base error raised by the canonical meeting Docs writer."""

    code = "docs.unavailable"
    retryable = True


class MeetingDocsIdentityConflict(MeetingDocsError):
    code = "docs.identity_conflict"
    retryable = False


def meeting_docs_node_id(job_id: UUID, artifact_type: ArtifactType) -> UUID:
    """Return the stable canonical node identity for one job artifact."""

    if artifact_type not in ("minutes", "memo"):
        raise ValueError(f"unsupported meeting artifact type: {artifact_type}")
    return uuid5(
        NAMESPACE_URL,
        f"https://aoitalk.local/meeting-processing/v1/jobs/{UUID(str(job_id))}/{artifact_type}",
    )


def _job_attr(job: Any, name: str, default: Any = None) -> Any:
    return getattr(job, name, default)


class MeetingDocsService:
    """Write meeting minutes/memos through the shared Docs operation boundary."""

    def __init__(
        self,
        session: AsyncSession | None = None,
        *,
        workspace_root: str | None = None,
        get_db_manager: Any | None = None,
    ):
        self.session = session
        self.workspace_root = workspace_root
        self.get_db_manager = get_db_manager

    async def ready(self) -> bool:
        """Return false unless the Docs schema and default Meeting tag exist.

        A bare ``SELECT 1`` only proves that a connection is alive.  Meeting
        processing cannot persist anything when one of the canonical Docs
        tables is missing (or when the seeded ``Meeting`` supertag was not
        created), so readiness probes every table used by this writer and the
        tag that ``persist_artifact`` attaches.
        """

        session = self.session
        owns_session = False
        if session is None and self.get_db_manager is not None:
            try:
                manager = self.get_db_manager
                manager = manager() if callable(manager) else manager
                if inspect.isawaitable(manager):
                    manager = await manager
                if manager is not None:
                    session = manager.get_session()
                    if inspect.isawaitable(session):
                        session = await session
                    owns_session = session is not None
            except Exception:
                session = None
        if session is None:
            return False
        try:
            # Selecting a column from each mapped table makes a missing table
            # fail closed while remaining portable across PostgreSQL/SQLite.
            await session.execute(select(DocsLibrary.id).limit(1))
            await session.execute(select(KnowledgeNode.id).limit(1))
            await session.execute(select(KnowledgeNodeSupertag.node_id).limit(1))
            await session.execute(select(KnowledgeSupertag.id).limit(1))
            tag_result = await session.execute(
                select(KnowledgeSupertag.id)
                .where(KnowledgeSupertag.system_key == "meeting")
                .limit(1)
            )
            return tag_result.scalar_one_or_none() is not None
        except Exception:
            try:
                await session.rollback()
            except Exception:
                pass
            return False
        finally:
            if owns_session:
                try:
                    await session.close()
                except Exception:
                    pass

    @staticmethod
    def _body(*, job: MeetingProcessingJob, artifact_type: ArtifactType, meeting_title: str, markdown: str) -> dict[str, Any]:
        label = "議事録" if artifact_type == "minutes" else "議事メモ"
        return {
            "format": "doc_block",
            "block_type": "markdown",
            "label": label,
            "content": str(markdown),
            "meeting_processing": {
                "contract_version": "1.0",
                "job_id": str(job.id),
                "artifact_type": artifact_type,
                "request_sha256": str(_job_attr(job, "request_sha256", "")),
                "audio_sha256": str(_job_attr(job, "audio_sha256", "")),
            },
        }

    @staticmethod
    def _provenance_matches(node: KnowledgeNode, *, job: MeetingProcessingJob, artifact_type: ArtifactType, library_id: UUID) -> bool:
        if node.docs_library_id != library_id or node.archived_at is not None:
            return False
        body = node.body_json if isinstance(node.body_json, Mapping) else {}
        provenance = body.get("meeting_processing")
        if not isinstance(provenance, Mapping):
            return False
        return (
            str(provenance.get("contract_version", "")) == "1.0"
            and str(provenance.get("job_id", "")) == str(job.id)
            and str(provenance.get("artifact_type", "")) == artifact_type
            and str(provenance.get("request_sha256", "")) == str(_job_attr(job, "request_sha256", ""))
            and str(provenance.get("audio_sha256", "")) == str(_job_attr(job, "audio_sha256", ""))
        )

    async def _node_for_update(self, session: AsyncSession, node_id: UUID) -> KnowledgeNode | None:
        result = await session.execute(
            select(KnowledgeNode).where(KnowledgeNode.id == node_id).with_for_update()
        )
        return result.scalar_one_or_none()

    async def _attach_meeting_tag(self, session: AsyncSession, *, service: DocsGraphService, node: KnowledgeNode, library_id: UUID, user_id: UUID) -> None:
        """Attach existing Meeting/system_key=meeting exactly once."""

        tag = await service.resolve_supertag(
            docs_library_id=library_id,
            tag="meeting",
            create=False,
        )
        link = await session.get(
            KnowledgeNodeSupertag,
            {"node_id": node.id, "supertag_id": tag.id},
        )
        if link is not None:
            return
        try:
            await apply_docs_operation(
                session,
                service,
                user_id=user_id,
                docs_library_id=library_id,
                table="knowledge_node_supertags",
                action="create",
                entity_id=f"{node.id}:{tag.id}",
                payload={"node_id": str(node.id), "supertag_id": str(tag.id)},
            )
        except Exception:
            # Another retry may have inserted the same composite link between
            # our read and canonical write.  Re-read after the operation's
            # transaction boundary and treat an existing link as success.
            await session.rollback()
            if await session.get(
                KnowledgeNodeSupertag,
                {"node_id": node.id, "supertag_id": tag.id},
            ) is None:
                raise

    async def persist_artifact(
        self,
        session: AsyncSession,
        *,
        job: MeetingProcessingJob,
        artifact_type: ArtifactType,
        meeting_title: str,
        markdown: str,
    ) -> UUID:
        """Create/recover one canonical node and Meeting tag idempotently."""

        if artifact_type not in ("minutes", "memo"):
            raise ValueError(f"unsupported meeting artifact type: {artifact_type}")
        try:
            job_id = UUID(str(job.id))
            actor_id = UUID(str(job.actor_user_id))
        except (TypeError, ValueError) as exc:
            raise MeetingDocsError("meeting job identity is invalid") from exc
        if not str(markdown):
            raise MeetingDocsError("meeting artifact markdown is empty")

        library = await ensure_docs_library(session, owner_user_id=actor_id)
        library_id = UUID(str(library.id))
        node_id = meeting_docs_node_id(job_id, artifact_type)
        service = DocsGraphService(session, workspace_root=self.workspace_root)
        node = await self._node_for_update(session, node_id)
        if node is not None:
            if not self._provenance_matches(node, job=job, artifact_type=artifact_type, library_id=library_id):
                raise MeetingDocsIdentityConflict(
                    f"canonical Docs node {node_id} has conflicting provenance"
                )
            await self._attach_meeting_tag(session, service=service, node=node, library_id=library_id, user_id=actor_id)
            return node_id

        body = self._body(
            job=job,
            artifact_type=artifact_type,
            meeting_title=meeting_title,
            markdown=markdown,
        )
        title = f"{str(meeting_title).strip()[:240] or '会議'}｜{'議事録' if artifact_type == 'minutes' else '議事メモ'}"
        try:
            await apply_docs_operation(
                session,
                service,
                user_id=actor_id,
                docs_library_id=library_id,
                table="knowledge_nodes",
                action="create",
                entity_id=str(node_id),
                payload={
                    "id": str(node_id),
                    "title": title,
                    "node_type": "node",
                    "body_json": body,
                },
            )
        except Exception as exc:
            # A concurrent creator may have won the deterministic insert.  A
            # fresh locked read lets us reuse it only after exact verification.
            await session.rollback()
            node = await self._node_for_update(session, node_id)
            if node is None:
                raise MeetingDocsError("canonical Docs node creation failed") from exc
            if not self._provenance_matches(node, job=job, artifact_type=artifact_type, library_id=library_id):
                raise MeetingDocsIdentityConflict(
                    f"canonical Docs node {node_id} has conflicting provenance"
                ) from exc

        node = await self._node_for_update(session, node_id)
        if node is None:
            raise MeetingDocsError("canonical Docs node disappeared after create")
        if not self._provenance_matches(node, job=job, artifact_type=artifact_type, library_id=library_id):
            raise MeetingDocsIdentityConflict(f"canonical Docs node {node_id} has conflicting provenance")
        await self._attach_meeting_tag(session, service=service, node=node, library_id=library_id, user_id=actor_id)
        return node_id
