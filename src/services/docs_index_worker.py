"""Durable, coalescing index delivery shared by every database writer."""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from sqlalchemy import select, text, update

from ..memory.models.docs_agent import DocsIndexQueue
from .docs_consistency import DOCS_LOCK_NAMESPACE, contract_available

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
