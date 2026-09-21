from __future__ import annotations

import asyncio, inspect, logging, os, socket, uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable
from sqlalchemy import and_, or_, select, update
from ..api.meeting_processing_contract import MeetingProcessingResult
from ..memory.models import MeetingProcessingJob
log = logging.getLogger(__name__)

class MeetingProcessingLeaseLost(RuntimeError):
    """Raised when a claim is no longer owned by this worker."""


class MeetingProcessingStageError(RuntimeError):
    """Canonical serialisable error passed to the A1 job envelope."""

    def __init__(self, code: str, message: str, *, stage: str, retryable: bool, details: Mapping[str, Any] | None = None):
        super().__init__(message); self.code=code; self.message=message; self.stage=stage; self.retryable=bool(retryable); self.details=dict(details or {})

@dataclass
class MeetingProcessingClaim:
    job_id: uuid.UUID; token: str; owner: str; actor_user_id: Any=None; audio_upload_id: Any=None; stage: str="queued"
    # Heartbeat sets this event when the conditional lease update no longer
    # matches.  The execution task checks it before every durable side effect
    # so a stale worker stops instead of continuing expensive work.
    lease_lost: asyncio.Event = field(default_factory=asyncio.Event, repr=False, compare=False)
    @property
    def id(self): return self.job_id
    def __getitem__(self,key): return getattr(self,{"id":"job_id","lease_token":"token"}.get(key,key))

class MeetingProcessingWorker:
    MAX_ATTEMPTS=3
    def __init__(
        self,
        get_db_manager: Callable[[], Any],
        *,
        config=None,
        workspace_root: str | os.PathLike[str] | None = None,
        storage=None,
        whisper=None,
        local_llm=None,
        docs=None,
        poll_interval=1.0,
        lease_seconds=120.0,
        concurrency=1,
        owner=None,
    ):
        self.get_db_manager=get_db_manager; self.config=config; self.storage=storage; self.whisper=whisper; self.local_llm=local_llm; self.docs=docs
        self.workspace_root = workspace_root
        self.poll_interval = max(0.05, float(poll_interval))
        # Heartbeats intentionally run at max(5s, lease/3).  A 30 second
        # floor keeps the first heartbeat from arriving after a short lease
        # has already expired, while still allowing tests/operators to pass a
        # larger deployment-specific value.
        self.lease_seconds = max(30.0, float(lease_seconds))
        self.concurrency = max(1, int(concurrency))
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        self._stop = asyncio.Event()
        self._poll_task = None
        self._tasks = set()
        self._claims = {}
        self.is_running = False
        if self.storage is None:
            try:
                from .meeting_processing_storage import MeetingAudioStorage

                self.storage = MeetingAudioStorage(
                    workspace_root,
                    defer_staging_cleanup=True,
                )
                # The worker owns lifecycle cleanup.  Keep the storage GC
                # disabled for this instance even when it is constructed by
                # the worker rather than injected by the composition root.
                for attr, value in (("defer_staging_cleanup", True), ("disable_staging_gc", True)):
                    try:
                        setattr(self.storage, attr, value)
                    except Exception:
                        pass
            except Exception:
                # A tiny dependency-free worker test may not have the storage
                # stack available; execution then falls back to the durable
                # upload id path and readiness remains fail-closed.
                self.storage = None
        else:
            for attr, value in (("defer_staging_cleanup", True), ("disable_staging_gc", True)):
                try:
                    setattr(self.storage, attr, value)
                except Exception:
                    pass
    async def _new_session(self):
        manager=self.get_db_manager(); manager=await manager if inspect.isawaitable(manager) else manager
        if manager is None: raise RuntimeError("database manager is unavailable")
        session=manager.get_session(); return await session if inspect.isawaitable(session) else session
    async def start(self):
        if self._poll_task is not None and not self._poll_task.done(): return
        self._stop.clear(); self.is_running=True; self._poll_task=asyncio.create_task(self._poll_loop(),name="meeting-processing-worker")
    async def stop(self):
        self._stop.set(); self.is_running=False; poll,self._poll_task=self._poll_task,None
        if poll is not None:
            poll.cancel()
            try: await poll
            except asyncio.CancelledError: pass
        # Snapshot ownership before cancelling tasks; cancellation cleanup
        # removes completed claims from the registry.
        claims=list(self._claims.values())
        tasks=list(self._tasks)
        for t in tasks: t.cancel()
        if tasks: await asyncio.gather(*tasks,return_exceptions=True)
        self._tasks.clear(); self._claims.clear()
        for claim in claims:
            try: await self._requeue_claim(claim)
            except Exception: log.warning("meeting worker requeue failed",exc_info=True)
    async def readiness_snapshot(self):
        async def ready(obj):
            if obj is None:return False
            fn=getattr(obj,"ready",None) or getattr(obj,"readiness",None)
            if fn is None:return True
            try:
                value=fn(); return bool(await value if inspect.isawaitable(value) else value)
            except Exception:return False
        a,b,c=await asyncio.gather(ready(self.whisper),ready(self.local_llm),ready(self.docs)); return {"worker":self.is_running,"whisper":a,"local_llm":b,"docs":c}
    async def _poll_loop(self):
        while not self._stop.is_set():
            try:
                snap=await self.readiness_snapshot()
                if snap["whisper"] and snap["local_llm"] and snap["docs"]:
                    capacity=self.concurrency-len(self._tasks)
                    if capacity>0:
                        for claim in await self.claim_many(capacity):
                            self._claims[claim.job_id]=claim; task=asyncio.create_task(self._execute_claim(claim),name=f"meeting-processing:{claim.job_id}"); self._tasks.add(task); task.add_done_callback(self._tasks.discard)
            except asyncio.CancelledError: raise
            except Exception: log.exception("meeting-processing poll failed")
            try: await asyncio.wait_for(self._stop.wait(),timeout=self.poll_interval)
            except asyncio.TimeoutError: pass
    async def claim_one(self):
        rows=await self.claim_many(1); return rows[0] if rows else None
    async def claim_many(self,limit):
        session=await self._new_session(); claims=[]
        try:
            now=datetime.utcnow(); limit=max(1,int(limit)); queued=and_(MeetingProcessingJob.status=="queued",or_(MeetingProcessingJob.next_attempt_at.is_(None),MeetingProcessingJob.next_attempt_at<=now)); stale=and_(MeetingProcessingJob.status=="running",or_(MeetingProcessingJob.lease_expires_at.is_(None),MeetingProcessingJob.lease_expires_at<=now))
            result=await session.execute(select(MeetingProcessingJob).where(or_(queued,stale)).order_by(MeetingProcessingJob.created_at,MeetingProcessingJob.id).with_for_update(skip_locked=True).limit(limit))
            for row in list(result.scalars().all()):
                attempts=int(row.attempt_count or 0)
                if attempts>=self.MAX_ATTEMPTS:
                    row.status="failed"; row.retryable=False; row.stage=row.stage or "queued"; row.error_json={"code":"worker.recovery_exhausted","message":"recovery attempt limit exceeded","stage":row.stage,"retryable":False,"details":{}}; row.finished_at=row.updated_at=now; row.lease_owner=row.lease_token=row.lease_expires_at=row.heartbeat_at=None; continue
                token=uuid.uuid4().hex; row.status="running"; row.attempt_count=attempts+1; row.lease_owner=self.owner; row.lease_token=token; row.heartbeat_at=now; row.lease_expires_at=now+timedelta(seconds=self.lease_seconds); row.updated_at=now
                if (row.stage or "queued")=="queued": row.started_at=row.started_at or now
                row.error_json={}; claims.append(MeetingProcessingClaim(row.id,token,self.owner,row.actor_user_id,row.audio_upload_id,row.stage or "queued"))
            await session.commit(); return claims
        except Exception: await session.rollback(); raise
        finally: await session.close()
    async def _heartbeat(self,claim,stop):
        interval=max(5.,self.lease_seconds/3.)
        while not stop.is_set():
            try: await asyncio.wait_for(stop.wait(),timeout=interval)
            except asyncio.TimeoutError: pass
            if stop.is_set(): return
            session=await self._new_session()
            try:
                now=datetime.utcnow(); result=await session.execute(update(MeetingProcessingJob).where(MeetingProcessingJob.id==claim.job_id,MeetingProcessingJob.status=="running",MeetingProcessingJob.lease_token==claim.token,MeetingProcessingJob.lease_expires_at>now).values(heartbeat_at=now,lease_expires_at=now+timedelta(seconds=self.lease_seconds),updated_at=now)); await session.commit()
                if getattr(result,"rowcount",1)==0:
                    claim.lease_lost.set()
                    return
            except asyncio.CancelledError: raise
            except Exception: await session.rollback(); log.warning("meeting heartbeat failed",exc_info=True)
            finally: await session.close()
    async def _lock_owned_job(self,session,job_id,token):
        now=datetime.utcnow(); result=await session.execute(update(MeetingProcessingJob).where(MeetingProcessingJob.id==job_id,MeetingProcessingJob.status=="running",MeetingProcessingJob.lease_token==token,MeetingProcessingJob.lease_expires_at>now).values(updated_at=now,lease_expires_at=now+timedelta(seconds=self.lease_seconds)).execution_options(synchronize_session=False))
        if getattr(result,"rowcount",1)!=1: await session.rollback(); raise MeetingProcessingLeaseLost()
        row=await session.get(MeetingProcessingJob,job_id)
        if row is None: await session.rollback(); raise MeetingProcessingLeaseLost()
        return row

    @staticmethod
    def _assert_claim_active(claim: MeetingProcessingClaim) -> None:
        if claim.lease_lost.is_set():
            raise MeetingProcessingLeaseLost()
    async def _execute_claim(self,claim):
        stop=asyncio.Event(); heartbeat=asyncio.create_task(self._heartbeat(claim,stop))
        try: await self._execute(claim)
        except asyncio.CancelledError: raise
        except MeetingProcessingLeaseLost: pass
        except Exception as exc: await self._mark_failed(claim,exc)
        finally:
            stop.set(); heartbeat.cancel()
            try: await heartbeat
            except asyncio.CancelledError: pass
            self._claims.pop(claim.job_id,None)
    async def _load_job(self,job_id):
        session=await self._new_session()
        try:return await session.get(MeetingProcessingJob,job_id)
        finally:await session.close()
    async def _transition(self,claim,stage,result_json):
        self._assert_claim_active(claim)
        session=await self._new_session()
        try:
            row=await self._lock_owned_job(session,claim.job_id,claim.token); row.stage=stage; row.result_json=dict(result_json); row.updated_at=datetime.utcnow(); await session.commit()
        except Exception: await session.rollback(); raise
        finally: await session.close()
    async def _execute(self,claim):
        self._assert_claim_active(claim)
        job=await self._load_job(claim.job_id)
        if job is None: raise MeetingProcessingLeaseLost()
        self._assert_claim_active(claim)
        stage=str(job.stage or "queued")
        raw_checkpoint = getattr(job, "result_json", {}) or {}
        if not isinstance(raw_checkpoint, Mapping):
            raise MeetingProcessingStageError(
                "worker.checkpoint_corrupt",
                "meeting job checkpoint is not an object",
                stage=stage if stage in {"queued", "transcribing", "generating_minutes", "generating_memo", "persisting_minutes", "persisting_memo", "complete"} else "transcribing",
                retryable=False,
            )
        checkpoint=dict(raw_checkpoint)
        for key in ("transcript", "minutes", "memo"):
            if key in checkpoint and not isinstance(checkpoint[key], Mapping):
                raise MeetingProcessingStageError(
                    "worker.checkpoint_corrupt",
                    f"meeting job checkpoint field {key!r} is not an object",
                    stage=stage if stage in {"queued", "transcribing", "generating_minutes", "generating_memo", "persisting_minutes", "persisting_memo", "complete"} else "transcribing",
                    retryable=False,
                    details={"field": key},
                )
        valid_stages = {"queued", "transcribing", "generating_minutes", "generating_memo", "persisting_minutes", "persisting_memo", "complete"}

        def require_checkpoint_object(field: str, active_stage: str) -> Mapping[str, Any]:
            value = checkpoint.get(field)
            if not isinstance(value, Mapping):
                raise MeetingProcessingStageError(
                    "worker.checkpoint_corrupt",
                    f"meeting job checkpoint field {field!r} is missing",
                    stage=active_stage if active_stage in valid_stages else "transcribing",
                    retryable=False,
                    details={"field": field},
                )
            return value
        raw_request = getattr(job, "request_json", {}) or {}
        request=dict(raw_request) if isinstance(raw_request, Mapping) else {}
        meeting=request.get("meeting") if isinstance(request.get("meeting"),Mapping) else {}; language=meeting.get("language")
        if stage=="queued":
            stage="transcribing"
            claim.stage = stage
            await self._transition(claim,stage,checkpoint)
        try:
            audio_path=self._audio_path(job)
            if inspect.isawaitable(audio_path):audio_path=await audio_path
        except MeetingProcessingStageError:
            raise
        except Exception as exc:
            raise MeetingProcessingStageError(
                "audio.integrity_failed",
                "staged meeting audio failed integrity validation",
                stage="transcribing",
                retryable=False,
                details={"type": type(exc).__name__},
            ) from exc
        if stage=="transcribing" and "transcript" not in checkpoint:
            claim.stage = stage
            self._assert_claim_active(claim)
            if self.whisper is None: raise MeetingProcessingStageError("whisper.model_unavailable","Whisper unavailable",stage=stage,retryable=True)
            value=await self._invoke(self.whisper,("transcribe","process"),audio_path,language=language); self._assert_claim_active(claim); checkpoint["transcript"]=self._as_dict(value); stage="generating_minutes"; await self._transition(claim,stage,checkpoint)
        elif stage=="transcribing" and "transcript" in checkpoint:
            stage="generating_minutes"; await self._transition(claim,stage,checkpoint)
        transcript=checkpoint.get("transcript",{})
        if stage=="generating_minutes" and "minutes" not in checkpoint:
            transcript = require_checkpoint_object("transcript", stage)
            claim.stage = stage
            self._assert_claim_active(claim)
            value=await self._generate("minutes",transcript,meeting,job); self._assert_claim_active(claim); checkpoint["minutes"]=self._as_dict(value); digest=checkpoint["minutes"].get("source_digest");
            if digest:checkpoint["_source_digest"]=digest
            stage="generating_memo"; await self._transition(claim,stage,checkpoint)
        elif stage=="generating_minutes" and "minutes" in checkpoint:
            require_checkpoint_object("transcript", stage)
            require_checkpoint_object("minutes", stage)
            stage="generating_memo"; await self._transition(claim,stage,checkpoint)
        if stage=="generating_memo" and "memo" not in checkpoint:
            transcript = require_checkpoint_object("transcript", stage)
            require_checkpoint_object("minutes", stage)
            claim.stage = stage
            self._assert_claim_active(claim)
            value=await self._generate("memo",transcript,meeting,job,source_digest=checkpoint.get("_source_digest")); self._assert_claim_active(claim); checkpoint["memo"]=self._as_dict(value); checkpoint.setdefault("_source_digest",checkpoint["memo"].get("source_digest")); stage="persisting_minutes"; await self._transition(claim,stage,checkpoint)
        elif stage=="generating_memo" and "memo" in checkpoint:
            require_checkpoint_object("transcript", stage)
            require_checkpoint_object("minutes", stage)
            require_checkpoint_object("memo", stage)
            stage="persisting_minutes"; await self._transition(claim,stage,checkpoint)
        title=self._meeting_title(job,meeting)
        if stage=="persisting_minutes":
            require_checkpoint_object("transcript", stage)
            require_checkpoint_object("minutes", stage)
            claim.stage = stage
            minutes_checkpoint = checkpoint.get("minutes") if isinstance(checkpoint.get("minutes"), Mapping) else {}
            if not minutes_checkpoint.get("docs_node_id"):
                await self._persist(claim,job,"minutes",title,checkpoint["minutes"].get("markdown", ""),checkpoint=checkpoint,next_stage="persisting_memo")
            stage="persisting_memo"; await self._transition(claim,stage,checkpoint)
        if stage=="persisting_memo" and not ((checkpoint.get("memo") if isinstance(checkpoint.get("memo"), Mapping) else {}).get("docs_node_id")):
            require_checkpoint_object("transcript", stage)
            require_checkpoint_object("minutes", stage)
            require_checkpoint_object("memo", stage)
            claim.stage = stage
            await self._persist(claim,job,"memo",title,checkpoint["memo"].get("markdown", ""),checkpoint=checkpoint,next_stage="complete")
        minutes_checkpoint = checkpoint.get("minutes") if isinstance(checkpoint.get("minutes"), Mapping) else {}
        memo_checkpoint = checkpoint.get("memo") if isinstance(checkpoint.get("memo"), Mapping) else {}
        require_checkpoint_object("transcript", "complete")
        require_checkpoint_object("minutes", "complete")
        require_checkpoint_object("memo", "complete")
        clean={"transcript":{k:transcript.get(k) for k in ("text","language","duration_seconds")},"minutes":{"title":title,"markdown":minutes_checkpoint.get("markdown", ""),"docs_node_id":minutes_checkpoint.get("docs_node_id")},"memo":{"title":title,"markdown":memo_checkpoint.get("markdown", ""),"docs_node_id":memo_checkpoint.get("docs_node_id")}}
        self._assert_claim_active(claim)
        for artifact_type, artifact in (("minutes", minutes_checkpoint), ("memo", memo_checkpoint)):
            node_value = artifact.get("docs_node_id")
            try:
                node_uuid = uuid.UUID(str(node_value))
            except (TypeError, ValueError, AttributeError) as exc:
                raise MeetingProcessingStageError("worker.checkpoint_corrupt", f"{artifact_type} Docs node id is invalid", stage="complete", retryable=False, details={"field": f"{artifact_type}.docs_node_id"}) from exc
            if not str(node_value).strip():
                raise MeetingProcessingStageError("worker.checkpoint_corrupt", f"{artifact_type} Docs node id is empty", stage="complete", retryable=False, details={"field": f"{artifact_type}.docs_node_id"})
            artifact["docs_node_id"] = str(node_uuid)
            clean[artifact_type]["docs_node_id"] = str(node_uuid)
        try: validated=MeetingProcessingResult.model_validate(clean)
        except Exception as exc: raise MeetingProcessingStageError("worker.checkpoint_corrupt","success checkpoint incomplete",stage="complete",retryable=False) from exc
        session=await self._new_session()
        try:
            row=await self._lock_owned_job(session,claim.job_id,claim.token); row.result_json=validated.model_dump(mode="json"); row.error_json={}; row.stage="complete"; row.status="succeeded"; row.retryable=False; row.finished_at=row.updated_at=datetime.utcnow(); row.lease_owner=row.lease_token=row.lease_expires_at=row.heartbeat_at=None; await session.commit()
        except Exception: await session.rollback(); raise
        finally: await session.close()
        # Staging data is retained through every retry/failure and removed
        # only after the durable succeeded commit.  Cleanup is deliberately
        # best effort: a filesystem failure must never turn a committed job
        # back into a failed one.
        await self._cleanup_staging(job)
    def _audio_path(self,job):
        if self.storage is None:return Path(str(getattr(job,"audio_upload_id", "")))
        value=self.storage.resolve_upload(job.actor_user_id,job.audio_upload_id)
        if inspect.isawaitable(value):
            async def resolve_async():
                resolved = await value
                return self._validate_resolved_upload(job, resolved)
            return resolve_async()
        return self._validate_resolved_upload(job, value)

    @staticmethod
    def _validate_resolved_upload(job, value):
        expected_name = str(getattr(job, "audio_file_name", "") or "")
        expected_size_raw = getattr(job, "audio_size_bytes", None)
        expected_sha = str(getattr(job, "audio_sha256", "") or "")
        try:
            expected_size = int(expected_size_raw)
        except (TypeError, ValueError) as exc:
            raise MeetingProcessingStageError("audio.integrity_failed", "meeting job audio size snapshot is invalid", stage="transcribing", retryable=False, details={"field": "audio_size_bytes"}) from exc
        actual_name = getattr(value, "file_name", None)
        actual_size = getattr(value, "size_bytes", None)
        actual_sha = getattr(value, "sha256", None)
        mismatches = []
        if actual_name != expected_name: mismatches.append("file_name")
        try:
            if int(actual_size) != expected_size: mismatches.append("size_bytes")
        except (TypeError, ValueError): mismatches.append("size_bytes")
        if str(actual_sha or "") != expected_sha: mismatches.append("sha256")
        if mismatches:
            raise MeetingProcessingStageError("audio.integrity_failed", "staged meeting audio does not match immutable job snapshot", stage="transcribing", retryable=False, details={"fields": mismatches})
        # ClipUpload intentionally exposes only payload_path as its internal
        # filesystem accessor; never stringify the metadata dataclass into a
        # path (that would make every real claim fail after staging).
        for key in ("payload_path", "path", "file_path", "staged_path", "source_path"):
            candidate = getattr(value, key, None)
            if candidate is not None:
                return Path(candidate)
        raise MeetingProcessingStageError("audio.integrity_failed", "resolved meeting upload has no payload path", stage="transcribing", retryable=False)

    async def _cleanup_staging(self, job: Any) -> None:
        storage = self.storage
        cleanup = getattr(storage, "cleanup_uploads", None) if storage is not None else None
        if not callable(cleanup):
            return
        try:
            result = cleanup(
                getattr(job, "actor_user_id", None),
                [getattr(job, "audio_upload_id", None)],
            )
            if inspect.isawaitable(result):
                await result
        except Exception:
            log.warning("meeting staging cleanup failed", exc_info=True)
    async def _generate(self,kind,transcript,meeting,job,**kwargs):
        if self.local_llm is None:raise MeetingProcessingStageError("local_llm.unavailable","local LLM unavailable",stage=f"generating_{kind}",retryable=True)
        payload={"transcript":transcript,"meeting":meeting,"job_id":str(job.id),**kwargs}; return await self._invoke(self.local_llm,(f"generate_{kind}","generate","complete"),payload,kind=kind,transcript=transcript,meeting=meeting,**kwargs)
    async def _persist(self,claim,job,kind,title,markdown,*,checkpoint,next_stage):
        if self.docs is None:raise MeetingProcessingStageError("docs.unavailable","Docs unavailable",stage=f"persisting_{kind}",retryable=True)
        self._assert_claim_active(claim)
        session=await self._new_session()
        try:
            owned=await self._lock_owned_job(session,claim.job_id,claim.token)
            # Docs adapters are expected to support ``defer_commit``.  Keep a
            # worker-side guard as well so a legacy/test adapter that calls
            # ``session.commit()`` directly cannot publish a side effect before
            # this job checkpoint is written.
            original_commit = session.commit
            async def flush_only() -> None:
                await session.flush()
            session.commit = flush_only  # type: ignore[method-assign]
            try:
                result=await self._invoke(self.docs,("persist_artifact","persist"),session,job=owned,artifact_type=kind,meeting_title=title,markdown=markdown,defer_commit=True)
            finally:
                session.commit = original_commit  # type: ignore[method-assign]
            self._assert_claim_active(claim)
            try:
                node_uuid = uuid.UUID(str(result))
            except (TypeError, ValueError, AttributeError) as exc:
                raise MeetingProcessingStageError("worker.checkpoint_corrupt", f"{kind} Docs node id is invalid", stage=f"persisting_{kind}", retryable=False, details={"field": f"{kind}.docs_node_id"}) from exc
            artifact = checkpoint.get(kind)
            if not isinstance(artifact, dict):
                raise MeetingProcessingStageError("worker.checkpoint_corrupt", f"meeting checkpoint field {kind!r} is missing", stage=f"persisting_{kind}", retryable=False, details={"field": kind})
            artifact["title"] = title
            artifact["docs_node_id"] = str(node_uuid)
            # A duplicate/race recovery path may have rolled back the
            # original transaction inside the Docs service.  Reacquire the
            # exact lease before recording the checkpoint so no stale worker
            # can commit a durable stage transition.
            owned=await self._lock_owned_job(session,claim.job_id,claim.token)
            owned.stage=next_stage
            owned.result_json=dict(checkpoint)
            owned.updated_at=datetime.utcnow()
            claim.stage=next_stage
            await session.commit()
            return node_uuid
        except MeetingProcessingStageError:
            await session.rollback(); raise
        except Exception as exc:
            await session.rollback()
            code = str(getattr(exc, "code", "docs.unavailable"))
            retryable = bool(getattr(exc, "retryable", True))
            raise MeetingProcessingStageError(code, str(exc) or "Docs persistence failed", stage=f"persisting_{kind}", retryable=retryable) from exc
        finally: await session.close()
    async def _requeue_claim(self,claim):
        session=await self._new_session()
        try:
            now=datetime.utcnow(); await session.execute(update(MeetingProcessingJob).where(MeetingProcessingJob.id==claim.job_id,MeetingProcessingJob.status=="running",MeetingProcessingJob.lease_token==claim.token,MeetingProcessingJob.lease_expires_at>now).values(status="queued",lease_owner=None,lease_token=None,lease_expires_at=None,heartbeat_at=None,next_attempt_at=now,updated_at=now)); await session.commit()
        except Exception: await session.rollback()
        finally: await session.close()
    async def _mark_failed(self,claim,exc):
        # Once heartbeat fencing reports a lost lease, this task must not
        # overwrite the successor's state.  The conditional lease update
        # below is still the final guard for a race after this check.
        if claim.lease_lost.is_set():
            return
        if isinstance(exc,MeetingProcessingStageError):
            err=exc
        elif getattr(exc,"code",None):
            # Service adapters may expose the same wire-shaped error class
            # without importing this module (for example during an optional
            # dependency bootstrap).  Preserve all structured fields rather
            # than collapsing them into worker.internal_error.
            stage = str(getattr(exc, "stage", "") or "")
            valid_stages = {"queued", "transcribing", "generating_minutes", "generating_memo", "persisting_minutes", "persisting_memo", "complete"}
            if stage not in valid_stages:
                stage = claim.stage if claim.stage in valid_stages else "transcribing"
            err=MeetingProcessingStageError(
                str(getattr(exc, "code", "worker.internal_error")),
                str(getattr(exc, "message", str(exc))) or "meeting processing failed",
                stage=stage,
                retryable=bool(getattr(exc,"retryable",True)),
                details=getattr(exc, "details", None),
            )
        else:
            valid_stages = {"queued", "transcribing", "generating_minutes", "generating_memo", "persisting_minutes", "persisting_memo", "complete"}
            err=MeetingProcessingStageError("worker.internal_error","meeting processing failed",stage=claim.stage if claim.stage in valid_stages else "transcribing",retryable=True)
        session=await self._new_session()
        try:
            row=await self._lock_owned_job(session,claim.job_id,claim.token)
            now=datetime.utcnow()
            row.status="failed"
            row.stage=err.stage
            row.retryable=err.retryable
            # Assign through the encrypted hybrid-property setter.  A bulk
            # UPDATE against ``error_json`` would write plaintext to the
            # backing ``_error_json`` column and bypass field encryption.
            row.error_json={"code":err.code,"message":err.message,"stage":err.stage,"retryable":err.retryable,"details":err.details}
            row.finished_at=now
            row.updated_at=now
            row.lease_owner=row.lease_token=row.lease_expires_at=row.heartbeat_at=None
            await session.commit()
        except MeetingProcessingLeaseLost:
            await session.rollback()
        except Exception: await session.rollback()
        finally: await session.close()
    @staticmethod
    async def _invoke(obj,names,*args,**kwargs):
        for name in names:
            fn=getattr(obj,name,None)
            if fn is None:continue
            signature = None
            try:
                signature = inspect.signature(fn)
                params=signature.parameters
                if not any(p.kind==p.VAR_KEYWORD for p in params.values()):kwargs={k:v for k,v in kwargs.items() if k in params}
            except (TypeError,ValueError):pass
            # Only fall back to keyword invocation when signature binding
            # itself rejects the positional shape.  A TypeError raised
            # *inside* a service must not trigger a duplicate external call.
            if signature is not None:
                try:
                    signature.bind(*args, **kwargs)
                except TypeError:
                    value = fn(**kwargs)
                else:
                    value = fn(*args, **kwargs)
            else:
                value = fn(*args, **kwargs)
            return await value if inspect.isawaitable(value) else value
        raise RuntimeError(f"service method unavailable: {names}")
    @staticmethod
    def _as_dict(value):
        if isinstance(value,Mapping):return dict(value)
        if hasattr(value,"__dict__"):return dict(vars(value))
        return {"text":str(value)}
    @staticmethod
    def _meeting_title(job,meeting):
        title=meeting.get("title_hint") or (getattr(job,"started_at",None).strftime("%Y-%m-%d 会議") if getattr(job,"started_at",None) else None) or Path(str(getattr(job,"audio_file_name", ""))).stem or "会議"; return str(title)[:240]

__all__=["MeetingProcessingClaim","MeetingProcessingLeaseLost","MeetingProcessingStageError","MeetingProcessingWorker"]
