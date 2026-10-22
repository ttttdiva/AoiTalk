"""Restart-safe Daily organizer with bounded leases and no external tools."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, or_, select

from ..memory.models import DocsDailyCapture
from .daily_capture_service import DailyCaptureConflict, DailyCaptureService, parse_plan
from .docs_consistency import DocsConflict

logger = logging.getLogger(__name__)


class DailyCaptureWorker:
    # A model call is bounded below the lease; crashed attempts become failed
    # and are explicitly retryable rather than looping on an unavailable model.
    LEASE_SECONDS = 180
    MODEL_TIMEOUT = 120

    def __init__(self, get_db_manager, *, plan_llm_factory=None, poll_interval=2.0):
        self.get_db_manager = get_db_manager
        self.plan_llm_factory = plan_llm_factory
        self.poll_interval = poll_interval
        self._task = None

    async def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="daily-capture-worker")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self):
        while True:
            try:
                if await self.run_once():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Daily organizer poll failed")
            await asyncio.sleep(self.poll_interval)

    async def claim(self):
        session = await self.get_db_manager().get_session()
        try:
            now = datetime.utcnow()
            job = (await session.execute(select(DocsDailyCapture).where(or_(
                DocsDailyCapture.status == "queued",
                and_(DocsDailyCapture.status == "running", or_(DocsDailyCapture.lease_expires_at <= now, DocsDailyCapture.lease_expires_at.is_(None))),
            )).order_by(DocsDailyCapture.created_at, DocsDailyCapture.id).with_for_update(skip_locked=True).limit(1))).scalar_one_or_none()
            if job is None:
                return None
            if job.status == "running":
                self._fail(job, "整理が中断されました。原文は保存済みです。再試行できます")
                await session.commit()
                return (None, None)
            token = str(uuid4())
            job.status = "running"
            job.attempt_count += 1
            job.lease_token = token
            job.lease_expires_at = now + timedelta(seconds=self.LEASE_SECONDS)
            job.updated_at = now
            await session.commit()
            return job.id, token
        finally:
            await session.close()

    @staticmethod
    def _fail(job, message):
        history = job.history or {}
        attempts = list(history.get("attempts", []))
        attempts.append({"attempt": job.attempt_count, "status": "failed", "finished_at": datetime.utcnow().isoformat(), "error_message": message})
        job.history = {**history, "attempts": attempts, "error_message": message}
        job.status = "failed"
        job.lease_token = None
        job.lease_expires_at = None
        job.updated_at = datetime.utcnow()

    @staticmethod
    def owns(job, token):
        return (job is not None and job.status == "running" and job.lease_token == token
                and job.lease_expires_at is not None and job.lease_expires_at > datetime.utcnow())

    async def run_once(self):
        claim = await self.claim()
        if claim is None:
            return False
        job_id, token = claim
        if job_id is None:
            return True
        try:
            session = await self.get_db_manager().get_session()
            try:
                job = await session.get(DocsDailyCapture, job_id)
                if not self.owns(job, token):
                    return True
                actor = job.actor_user_id
                prepared = await DailyCaptureService(session).prepare(job)
                await session.commit()
            finally:
                await session.close()
            if self.plan_llm_factory is None:
                raise RuntimeError("Daily organizer model unavailable")
            async def generate():
                async with self.plan_llm_factory(user_id=str(actor), session_id=None, project_id=None) as llm:
                    return await llm(prepared["prompt"])
            raw = await asyncio.wait_for(generate(), timeout=self.MODEL_TIMEOUT)
            plan = parse_plan(raw, prepared["tag_ids"], prepared["link_ids"])
            session = await self.get_db_manager().get_session()
            try:
                job = (await session.execute(select(DocsDailyCapture).where(DocsDailyCapture.id == job_id).with_for_update())).scalar_one_or_none()
                if not self.owns(job, token):
                    return True
                deadline = job.lease_expires_at
                await DailyCaptureService(session).apply_plan(job, prepared, plan)
                # The job row stayed locked throughout apply. Final fencing
                # also rejects a slow mutation that outlived the claim lease.
                if datetime.utcnow() >= deadline:
                    raise DailyCaptureConflict("整理の期限が切れました")
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
            finally:
                await session.close()
        except asyncio.CancelledError:
            # Leave the durable running row for expired-lease recovery.
            raise
        except Exception as error:
            # Never persist provider output/URLs, credentials, or private
            # prompt snippets from an exception into the user-facing journal.
            message = (str(error) if isinstance(error, DailyCaptureConflict) else
                       "ノートや権限が変わりました。原文は保存済みです。再試行してください" if isinstance(error, (DocsConflict, PermissionError)) else
                       "整理できませんでした。原文は保存済みです。モデル設定を確認して再試行できます")
            session = await self.get_db_manager().get_session()
            try:
                job = (await session.execute(select(DocsDailyCapture).where(DocsDailyCapture.id == job_id).with_for_update())).scalar_one_or_none()
                if job is not None and job.status == "running" and job.lease_token == token:
                    self._fail(job, message)
                    await session.commit()
            finally:
                await session.close()
        return True
