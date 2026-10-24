"""Durable, coalescing index delivery shared by every database writer."""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from sqlalchemy import select, text, update

from ..memory.models.docs_agent import DocsIndexQueue
from .docs_consistency import DOCS_LOCK_NAMESPACE, contract_available
from .embedding_feature import runtime_embedding_enabled
from .gpu_admission import get_gpu_admission

logger = logging.getLogger(__name__)


class DocsIndexWorker:
    def __init__(self, session_factory, index=None, interval=5.0):
        self.session_factory = session_factory
        self.index = index
        self.interval = interval
        self.task = None

    async def start(self):
        if self.task is None:
            from ..rag import docs_index
            docs_index._durable_worker_active = True
            self.task = asyncio.create_task(self._run(), name="docs-index-delivery")

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        from ..rag import docs_index
        docs_index._durable_worker_active = False

    async def run_once(self):
        """Reindex one library under a GPU job ticket; no lock or session while a lease holds the GPU."""
        # Master switch OFF: return before the GPU ticket so no admission,
        # lock, session or queue write happens; in-flight work is untouched.
        if not runtime_embedding_enabled():
            return False
        admission = get_gpu_admission()
        ticket = admission.admit_job()
        if ticket is None:
            return False
        try:
            return await self._deliver_once()
        finally:
            admission.finish_job(ticket)

    async def _deliver_once(self):
        # Master switch OFF: no session, lock, index or queue write, even for a
        # cached/injected Docs index; the reason field is untouched.
        if not runtime_embedding_enabled():
            return False
        from ..rag.docs_index import docs_rag_enabled, get_docs_index_service
        if self.index is None and not docs_rag_enabled():
            return False
        session = await self.session_factory()
        try:
            if not await contract_available(session):
                return False
            acquired = await session.scalar(text("SELECT pg_try_advisory_xact_lock(:namespace,2)"),
                                            {"namespace": DOCS_LOCK_NAMESPACE})
            if not acquired:
                return False
            row = (await session.execute(select(DocsIndexQueue).where(
                DocsIndexQueue.requested_revision > DocsIndexQueue.applied_revision,
            ).order_by(DocsIndexQueue.updated_at, DocsIndexQueue.library_id).limit(1))).scalar_one_or_none()
            if row is None:
                return False
            library_id, requested = row.library_id, row.requested_revision
            index = self.index or get_docs_index_service()
            if self.index is None and not await index.initialize_for_reindex():
                return False
            report = await index.reconcile_library(session, library_id)
            if report.get("status") != "synced":
                return False
            # Next.js attachment writes share this durable database queue.
            # Publish attachment jobs only after the corresponding reconcile commits.
            from ..memory.models import DocsLibrary, KnowledgeAttachment, KnowledgeNode
            from .media_index_worker import queue_media_after_commit
            library = await session.get(DocsLibrary, library_id)
            attachments = (await session.execute(select(KnowledgeAttachment).join(
                KnowledgeNode, KnowledgeAttachment.node_id == KnowledgeNode.id,
            ).where(KnowledgeNode.docs_library_id == library_id))).scalars().all()
            for attachment in attachments:
                actor = attachment.created_by or (library.owner_user_id if library else None)
                if actor:
                    queue_media_after_commit(session, actor, "docs_attachment", attachment.id)
            # A writer may have advanced requested_revision while embedding.
            # Acknowledge only the revision observed before this job started.
            await session.execute(update(DocsIndexQueue).where(
                DocsIndexQueue.library_id == library_id,
            ).values(applied_revision=requested))
            await session.commit()
            return True
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def _run(self):
        delay = self.interval
        while True:
            try:
                await self.run_once()
                delay = self.interval
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Docs index delivery deferred; durable queue retained", exc_info=True)
                delay = min(60.0, max(self.interval, delay * 2))
            await asyncio.sleep(delay)
