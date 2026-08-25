"""Durable PostgreSQL worker for Docs clip ingestion.

The worker deliberately keeps the durable job lock short. Preparation (LLM,
network and staging-file reads) runs without holding a database session; the
read-only planner phase opens a short session through ``plan_session_factory``.
The final Docs mutation owns only the target-node locks taken by
``ClipIngestService``. A lease token fences every transition so a restarted
worker cannot publish an old result.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import logging
import math
import os
import socket
import uuid
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

from sqlalchemy import and_, or_, select, text, update

from ..memory.models import DocsClipIngestJob, DocsLibrary, KnowledgeNode
from .clip_ingest_service import ClipIngestError
from .clip_ingest_storage import ClipIngestStorage
from .docs_ingest_service import (
    DocsIngestService,
    DocsIngestUnavailableError,
    PreparedDocsIngest,
)

logger = logging.getLogger(__name__)


# ``clip_ingest_route`` is request-scoped runtime metadata.  It may be
# produced by a context-managed LLM factory, but it must never carry the
# factory's client (or any other arbitrary object) into ``prepare``.  Keep the
# route deliberately narrow and JSON-compatible; the client itself is passed
# as a live object only for the duration of the preparation callback.
_SAFE_CLIP_INGEST_ROUTE_KEYS = frozenset(
    {
        "inherit",
        "provider",
        "model",
        "reasoning_effort",
        "mode",
        "base_url",
        "allow_main_fallback",
        "fallback_to_main",
        "allow_fallback",
    }
)


class _LeaseLostError(RuntimeError):
    """The job lease was fenced while a claim was being executed."""


class DocsClipIngestWorker:
    """Poll, claim, execute and recover durable ClipIngest jobs."""

    # A stale lease is recoverable a bounded number of times.  Once this cap
    # has already been consumed, claiming again would otherwise create an
    # unbounded running -> stale -> running loop after a persistent failure.
    MAX_ATTEMPTS = 3

    def __init__(
        self,
        get_db_manager: Callable[[], Any],
        *,
        plan_llm_factory: Callable[..., AbstractAsyncContextManager[Any]] | None = None,
        # Kept for callers that have not moved to a scoped factory yet. New
        # server wiring should always provide ``plan_llm_factory``.
        plan_llm: Callable[[str], Awaitable[str]] | None = None,
        config: Any = None,
        workspace_root: str | os.PathLike[str] | None = None,
        storage: ClipIngestStorage | None = None,
        poll_interval: float = 1.0,
        lease_seconds: float = 60.0,
        concurrency: int = 2,
        owner: str | None = None,
    ) -> None:
        self.get_db_manager = get_db_manager
        self.plan_llm_factory = plan_llm_factory
        self.plan_llm = plan_llm
        self.config = config
        # A durable worker cannot safely remove promoted files on a failed
        # attempt.  The job row may still be retryable, a DB status transition
        # may be ambiguous, or a replacement worker may already own the same
        # deterministic upload.  Cleanup is therefore limited to the
        # post-commit staging path below; failure cleanup always retains both
        # staging and promotion artifacts.
        self._retain_side_effects_on_failure = True
        if storage is None:
            self.storage = ClipIngestStorage(
                workspace_root,
                defer_staging_cleanup=True,
            )
        else:
            self.storage = storage
            # Worker execution owns the durable transaction boundary.  Force
            # the worker-only mode even when an embedding supplies a storage
            # instance constructed with legacy synchronous defaults.
            try:
                setattr(self.storage, "defer_staging_cleanup", True)
            except Exception:
                # Tiny storage fakes may be immutable; service/cleanup paths
                # still use their historical behavior in that test-only case.
                pass
        # The API request boundary supplies a complete database-backed
        # protection set for opportunistic GC.  A worker must not run a
        # user-scoped sweep with only its current claim's IDs: another queued
        # job for the same actor could otherwise be deleted while this job is
        # preparing.  Durable worker cleanup is explicit and post-commit-only.
        try:
            setattr(self.storage, "disable_staging_gc", True)
        except Exception:
            pass
        self.poll_interval = max(0.05, float(poll_interval))
        # A lease shorter than the heartbeat/DB round-trip window can strand a
        # valid claim. Keep the public minimum aligned with the worker
        # contract even when an environment setting is too small.
        self.lease_seconds = max(30.0, float(lease_seconds))
        self.concurrency = max(1, int(concurrency))
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        self._stop = asyncio.Event()
        self._poll_task: asyncio.Task[Any] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    async def start(self) -> None:
        """Start one idempotent poll loop."""

        if self._poll_task is not None and not self._poll_task.done():
            return
        self._stop.clear()
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name="docs-clip-ingest-worker"
        )

    async def stop(self) -> None:
        """Stop polling and cancel in-flight claims without holding locks."""

        self._stop.set()
        poll = self._poll_task
        self._poll_task = None
        if poll is not None:
            poll.cancel()
            try:
                await poll
            except asyncio.CancelledError:
                pass

        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _new_session(self):
        manager = self.get_db_manager()
        if manager is None:
            raise RuntimeError("database manager is unavailable")
        return await manager.get_session()

    async def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                capacity = self.concurrency - len(self._tasks)
                if capacity > 0:
                    claims = await self.claim_many(capacity)
                    for claim in claims:
                        task = asyncio.create_task(
                            self._execute_claim(claim),
                            name=f"docs-clip-ingest:{claim['id']}",
                        )
                        self._tasks.add(task)
                        task.add_done_callback(self._tasks.discard)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Docs ClipIngest worker poll failed")

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                continue

    async def claim_one(self) -> dict[str, Any] | None:
        """Claim one queued or expired running row (compatibility helper)."""

        claims = await self.claim_many(1)
        return claims[0] if claims else None

    async def claim_many(self, limit: int) -> list[dict[str, Any]]:
        """Claim up to *limit* rows in one short ``SKIP LOCKED`` transaction."""

        count = max(1, int(limit))
        session = await self._new_session()
        try:
            now = datetime.utcnow()
            stale = and_(
                DocsClipIngestJob.status == "running",
                or_(
                    DocsClipIngestJob.lease_expires_at.is_(None),
                    DocsClipIngestJob.lease_expires_at <= now,
                ),
            )
            result = await session.execute(
                select(DocsClipIngestJob)
                .where(or_(DocsClipIngestJob.status == "queued", stale))
                .order_by(DocsClipIngestJob.created_at, DocsClipIngestJob.id)
                .with_for_update(skip_locked=True)
                .limit(count)
            )
            rows = list(result.scalars().all())
            if not rows:
                await session.commit()
                return []

            claims: list[dict[str, Any]] = []
            for row in rows:
                current_attempts = int(row.attempt_count or 0)
                if current_attempts >= self.MAX_ATTEMPTS:
                    # Do not issue a fresh lease once the bounded recovery
                    # budget is exhausted.  Keep this payload deliberately
                    # generic; provider/URL details never belong in a job row.
                    row.status = "failed"
                    row.retryable = False
                    row.error_json = {
                        "category": "recovery_exhausted",
                        "code": "recovery_exhausted",
                        "message": "ClipIngest jobの復旧試行回数上限に達しました",
                        "retryable": False,
                    }
                    row.finished_at = now
                    row.updated_at = now
                    row.lease_owner = None
                    row.lease_token = None
                    row.lease_expires_at = None
                    row.heartbeat_at = None
                    continue
                token = uuid.uuid4().hex
                row.status = "running"
                row.attempt_count = current_attempts + 1
                row.started_at = now
                row.updated_at = now
                row.heartbeat_at = now
                row.lease_owner = self.owner
                row.lease_token = token
                row.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
                row.error_json = {}
                claims.append(self._claim_payload(row, token))

            await session.commit()
            return claims
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    @staticmethod
    def _claim_payload(row: Any, token: str) -> dict[str, Any]:
        request = getattr(row, "request_json", {})
        request = dict(request) if isinstance(request, Mapping) else {}
        scope = request.get("scope")
        scope = dict(scope) if isinstance(scope, Mapping) else {}
        scope.setdefault("user_id", str(getattr(row, "actor_user_id", "")))
        scope.setdefault("session_id", _string_or_none(getattr(row, "session_id", None)))
        scope.setdefault("project_id", _string_or_none(getattr(row, "project_id", None)))
        request["scope"] = scope
        return {
            "id": row.id,
            "token": token,
            "actor_user_id": row.actor_user_id,
            "docs_library_id": getattr(row, "docs_library_id", None),
            "session_id": getattr(row, "session_id", None),
            "project_id": getattr(row, "project_id", None),
            "source": row.source_text,
            "upload_ids": list(getattr(row, "upload_ids_json", None) or []),
            "target_node_id": getattr(row, "target_node_id", None),
            "request": request,
        }

    async def _heartbeat(
        self, job_id: uuid.UUID, token: str, stop: asyncio.Event
    ) -> None:
        interval = max(1.0, self.lease_seconds / 3.0)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                return

            session = None
            try:
                session = await self._new_session()
                now = datetime.utcnow()
                result = await session.execute(
                    update(DocsClipIngestJob)
                    .where(
                        DocsClipIngestJob.id == job_id,
                        DocsClipIngestJob.status == "running",
                        DocsClipIngestJob.lease_token == token,
                        # Never resurrect a lease that already expired.  A
                        # delayed heartbeat must lose to stale reclaim/fencing
                        # rather than extending an old worker's ownership.
                        DocsClipIngestJob.lease_expires_at > now,
                    )
                    .values(
                        heartbeat_at=now,
                        lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                        updated_at=now,
                    )
                )
                await session.commit()
                if getattr(result, "rowcount", 1) == 0:
                    # A different worker owns the row; stop extending the old
                    # lease instead of racing it forever.
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Docs ClipIngest heartbeat failed: %s", job_id, exc_info=True
                )
                if session is not None:
                    await session.rollback()
            finally:
                if session is not None:
                    await session.close()

    async def _execute_claim(self, claim: dict[str, Any]) -> None:
        job_id = claim["id"]
        token = claim["token"]
        heartbeat_stop = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(job_id, token, heartbeat_stop),
            name=f"docs-clip-ingest-heartbeat:{job_id}",
        )
        try:
            await self._execute(claim, token)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not isinstance(exc, _LeaseLostError):
                logger.exception("Docs ClipIngest job failed: %s", job_id)
            await self._mark_failed(job_id, token, exc)
        finally:
            heartbeat_stop.set()
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    async def _execute(self, claim: dict[str, Any], token: str) -> None:
        request = claim.get("request")
        request = dict(request) if isinstance(request, Mapping) else {}
        scope = request.get("scope")
        scope = dict(scope) if isinstance(scope, Mapping) else {}
        user_id = claim["actor_user_id"]
        snapshot_target = request.get("target")
        snapshot_target_id = (
            snapshot_target.get("node_id")
            if isinstance(snapshot_target, Mapping)
            else None
        )
        explicit_target_id = None
        session_context = request.get("session_context")
        project_metadata = request.get("project_metadata")
        privacy = request.get("privacy_context") or request.get("privacy")
        if isinstance(privacy, Mapping):
            if not isinstance(session_context, Mapping) and isinstance(
                privacy.get("session_context"), Mapping
            ):
                session_context = privacy.get("session_context")
            if not isinstance(project_metadata, Mapping) and isinstance(
                privacy.get("project_metadata"), Mapping
            ):
                project_metadata = privacy.get("project_metadata")
            privacy_mode = privacy.get("privacy_mode")
            if privacy_mode and not isinstance(session_context, Mapping):
                session_context = {"privacy_mode": str(privacy_mode)}
        if not isinstance(session_context, Mapping):
            session_context = None
        if not isinstance(project_metadata, Mapping):
            project_metadata = None

        # The request snapshot is immutable audit input, not an authorization
        # grant.  Resolve current session/project ACLs, target write access,
        # and privacy metadata before opening the LLM/URL preparation phase.
        scope, session_context, project_metadata = await self._preflight_scope(
            claim,
            request=request,
            scope=scope,
            session_context=session_context,
            project_metadata=project_metadata,
        )

        from .outbound_privacy_service import (
            reset_privacy_policy_context,
            set_privacy_policy_context,
        )
        from .turn_context import reset_turn_context, set_turn_context

        turn_token = set_turn_context(
            user_id=str(user_id),
            session_id=_string_or_none(scope.get("session_id")),
            project_id=_string_or_none(scope.get("project_id")),
            include_project_context=scope.get("include_project_context"),
        )
        privacy_token = set_privacy_policy_context(
            session_context=session_context,
            project_metadata=project_metadata,
        )
        prepared: PreparedDocsIngest | Any = None
        prep_service: DocsIngestService | None = None
        final_service: DocsIngestService | None = None
        result: Any = None
        commit_ambiguous = False
        try:
            if snapshot_target_id:
                try:
                    explicit_target_id = uuid.UUID(str(snapshot_target_id))
                except (TypeError, ValueError, AttributeError) as exc:
                    raise ClipIngestError("ClipIngest jobの保存先指定が不正です") from exc
                claim_target_id = claim.get("target_node_id")
                if claim_target_id is None:
                    raise ClipIngestError("ClipIngest jobの保存先が削除されているため実行できません")
                try:
                    if uuid.UUID(str(claim_target_id)) != explicit_target_id:
                        raise ClipIngestError("ClipIngest jobの保存先指定が変更されています")
                except (TypeError, ValueError, AttributeError) as exc:
                    raise ClipIngestError("ClipIngest jobの保存先指定が不正です") from exc
            # ``DocsIngestService.prepare`` owns the preparation boundary.  A
            # durable worker must not open a DB session before URL fetch,
            # supplemental research, image recognition, or planner LLM work:
            # those operations can block for an unbounded provider timeout.
            # Newer service versions receive a session factory and open a
            # short-lived session only around the local ``prepare_plan`` read.
            service_kwargs: dict[str, Any] = {
                "config": self.config,
                "session_context": dict(session_context) if session_context else None,
                "project_metadata": dict(project_metadata) if project_metadata else None,
                "storage": self.storage,
            }
            # The preparation-boundary API is accepted by the service
            # constructor in the current worker/service contract.  Keep the
            # signature check so the worker can fail closed if an older
            # service is accidentally imported instead of holding a session
            # across external I/O.
            try:
                service_parameters = inspect.signature(DocsIngestService).parameters
            except (TypeError, ValueError):
                service_parameters = {}
            if "plan_session_factory" in service_parameters:
                service_kwargs["plan_session_factory"] = self._new_session
            prep_service = DocsIngestService(None, **service_kwargs)
            request_route = _safe_clip_ingest_route(request.get("clip_ingest_route"))

            async def _prepare_with_llm(
                llm: Any,
                *,
                clip_ingest_route: dict[str, Any] | None = None,
                clip_ingest_client: Any = None,
            ) -> Any:
                route = _safe_clip_ingest_route(clip_ingest_route) or request_route
                prepare_kwargs: dict[str, Any] = {
                    "user_id": user_id,
                    "source": claim.get("source") or "",
                    "plan_llm": llm,
                    "upload_ids": claim.get("upload_ids") or [],
                    "upload_metadata": (
                        request.get("attachments")
                        if isinstance(request.get("attachments"), list)
                        else []
                    ),
                    "skip_image_recognition": bool(request.get("skip_image_recognition")),
                    "enable_external_research": bool(
                        request.get("enable_external_research", True)
                    ),
                    "target_node_id": claim.get("target_node_id"),
                    "clip_ingest_route": route,
                    # Never read a client from the encrypted request snapshot.
                    # It is not serializable and could be stale or attacker
                    # controlled.  Only the factory's live context may supply
                    # this runtime object.
                    "clip_ingest_client": clip_ingest_client,
                }
                prepare = prep_service.prepare
                try:
                    parameters = inspect.signature(prepare).parameters
                except (TypeError, ValueError):
                    parameters = {}
                if "plan_session_factory" in parameters:
                    # Accept the alternate method-level API used by an
                    # intermediate deployment; the constructor-level API
                    # below remains the canonical path.
                    prepare_kwargs["plan_session_factory"] = self._new_session
                    return await prepare(**prepare_kwargs)

                if (
                    prep_service.session is None
                    and callable(getattr(prep_service, "plan_session_factory", None))
                ):
                    return await prepare(**prepare_kwargs)
                # Never fall back to opening a session around the whole
                # ``prepare`` call: that would hold a DB connection across
                # URL/research/LLM I/O and reintroduce the durable-worker
                # lifecycle bug this boundary is intended to prevent.
                raise ClipIngestError(
                    "Docs planner用DB session factoryが設定されていません"
                )

            prepared = await self._with_plan_llm(
                user_id=user_id,
                scope=scope,
                session_context=session_context,
                project_metadata=project_metadata,
                callback=_prepare_with_llm,
            )

            # Verify the lease without locking the job row. The target rows
            # remain protected by ClipIngestService.apply_plan itself.
            session = await self._new_session()
            try:
                valid = await session.scalar(
                    select(DocsClipIngestJob.id).where(
                        DocsClipIngestJob.id == claim["id"],
                        DocsClipIngestJob.status == "running",
                        DocsClipIngestJob.lease_token == token,
                        DocsClipIngestJob.lease_expires_at > datetime.utcnow(),
                    )
                )
                if valid is None:
                    await session.rollback()
                    raise _LeaseLostError("Docs ClipIngest lease expired")

                await self._rebind_plan_nodes(prepared, session)
                if explicit_target_id is not None:
                    plan_target = getattr(getattr(prepared, "plan", None), "target", None)
                    plan_target_id = getattr(plan_target, "node_id", None)
                    try:
                        plan_target_matches = (
                            plan_target_id is not None
                            and uuid.UUID(str(plan_target_id)) == explicit_target_id
                        )
                    except (TypeError, ValueError, AttributeError):
                        plan_target_matches = False
                    if not plan_target_matches:
                        raise ClipIngestError("ClipIngest jobの保存先計画が変更されています")
                # Legacy synchronous POST /api/docs/ingest takes the same
                # advisory lock around its final mutation.  Keep preparation
                # parallel, but serialize only this short apply/commit window
                # so queued and legacy callers cannot race the same actor.
                bind = getattr(session, "bind", None)
                dialect = getattr(getattr(bind, "dialect", None), "name", None)
                if dialect == "postgresql":
                    await session.execute(
                        text(
                            "SELECT pg_advisory_xact_lock(hashtext('docs-ingest'), hashtext(:user_id))"
                        ),
                        {"user_id": str(user_id)},
                    )
                final_service = DocsIngestService(
                    session,
                    config=self.config,
                    session_context=dict(session_context) if session_context else None,
                    project_metadata=dict(project_metadata) if project_metadata else None,
                    storage=self.storage,
                )
                result = await final_service.finalize(user_id=user_id, prepared=prepared)
                snapshot = self._result_snapshot(result)
                receipt_id = _uuid_or_none(
                    getattr(result, "clip_ingest_receipt_id", None)
                    or getattr(result, "receipt_id", None)
                )
                now = datetime.utcnow()
                persisted = await session.execute(
                    update(DocsClipIngestJob)
                    .where(
                        DocsClipIngestJob.id == claim["id"],
                        DocsClipIngestJob.status == "running",
                        DocsClipIngestJob.lease_token == token,
                        DocsClipIngestJob.lease_expires_at > now,
                    )
                    .values(
                        status="succeeded",
                        result_json=snapshot,
                        receipt_id=receipt_id,
                        retryable=False,
                        finished_at=now,
                        updated_at=now,
                        lease_owner=None,
                        lease_token=None,
                        lease_expires_at=None,
                        heartbeat_at=None,
                    )
                )
                if getattr(persisted, "rowcount", 1) == 0:
                    await session.rollback()
                    raise _LeaseLostError("Docs ClipIngest lease fenced before commit")
                try:
                    await session.commit()
                except Exception:
                    # The server may have committed the transaction before a
                    # connection failure reached this worker.  Cleanup must
                    # inspect durable state and otherwise retain files.
                    commit_ambiguous = True
                    raise
                # Staging cleanup is intentionally outside the transaction:
                # only after the successful commit may the worker delete the
                # source sidecars/payloads.  Cleanup failure is fail-soft—the
                # durable success row remains authoritative and a later GC
                # pass can remove abandoned staging safely.
                await self._cleanup_staging_after_commit(
                    result,
                    final_service or prep_service,
                    user_id=user_id,
                    upload_ids=claim.get("upload_ids") or [],
                )
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()
        except Exception as exc:
            # Once the token has been fenced, a replacement worker may already
            # own the same staged/promotion handles. Never remove its files
            # from the old worker.
            if not isinstance(exc, _LeaseLostError):
                await self._cleanup_side_effects(
                    result,
                    final_service or prep_service,
                    user_id=user_id,
                    upload_ids=claim.get("upload_ids") or [],
                    job_id=claim["id"],
                    token=token,
                    commit_ambiguous=commit_ambiguous,
                )
            raise
        finally:
            reset_privacy_policy_context(privacy_token)
            reset_turn_context(turn_token)

    async def _rebind_plan_nodes(self, prepared: Any, session: Any) -> None:
        """Replace detached preparation nodes with this session's identities."""

        plan = getattr(prepared, "plan", None)
        if plan is None:
            return
        target = getattr(plan, "target", None)
        target_id = getattr(target, "node_id", None)
        if target is not None and target_id is not None:
            target_node = await session.get(KnowledgeNode, target_id)
            if target_node is None:
                raise ClipIngestError("保存直前の取り込み先を確認できません")
            target.node = target_node
        existing = getattr(plan, "existing_node", None)
        existing_id = getattr(existing, "id", None) if existing is not None else None
        if existing_id is not None:
            existing_node = await session.get(KnowledgeNode, existing_id)
            if existing_node is None:
                raise ClipIngestError("保存直前の追記先を確認できません")
            plan.existing_node = existing_node

    async def _preflight_scope(
        self,
        claim: Mapping[str, Any],
        *,
        request: Mapping[str, Any],
        scope: Mapping[str, Any],
        session_context: Mapping[str, Any] | None,
        project_metadata: Mapping[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
        """Revalidate immutable scope/ACL state before any external I/O.

        Queue snapshots are intentionally treated as hints.  The fresh job row,
        conversation/project ACLs, current Docs library, and explicit target
        are authoritative at execution time.  Privacy is merged monotonically
        so a current ``protected``/``local_only`` policy can only tighten an old
        snapshot, never be weakened by it.
        """

        preflight = await self._new_session()
        try:
            from .docs_acl import can_write_node, library_can_write
            from ..memory.conversation_repository import ConversationRepository
            from ..memory.project_repository import ProjectRepository

            job_id = claim.get("id")
            job = await preflight.get(DocsClipIngestJob, job_id)
            if job is None:
                raise ClipIngestError("ClipIngest jobが存在しません")
            if str(getattr(job, "status", "")) != "running":
                raise _LeaseLostError("Docs ClipIngest job lease is no longer active")
            if str(getattr(job, "lease_token", "") or "") != str(
                claim.get("token") or ""
            ):
                raise _LeaseLostError("Docs ClipIngest job lease was replaced")
            lease_expires_at = getattr(job, "lease_expires_at", None)
            if lease_expires_at is None or lease_expires_at <= datetime.utcnow():
                raise _LeaseLostError("Docs ClipIngest job lease expired")

            actor = _uuid_or_none(getattr(job, "actor_user_id", None))
            if actor is None:
                raise ClipIngestError("ClipIngest jobの実行ユーザーを確認できません")
            claim_actor = _uuid_or_none(claim.get("actor_user_id"))
            if claim_actor != actor:
                raise ClipIngestError("ClipIngest jobの実行ユーザー指定が変更されています")
            snapshot_user = _uuid_or_none(scope.get("user_id"))
            if snapshot_user is not None and snapshot_user != actor:
                raise ClipIngestError("ClipIngest scopeのユーザー指定が不正です")

            persisted_session = _uuid_or_none(getattr(job, "session_id", None))
            snapshot_session = _uuid_or_none(scope.get("session_id"))
            if persisted_session != snapshot_session:
                raise ClipIngestError("ClipIngest session scopeが変更されています")

            persisted_project = _uuid_or_none(getattr(job, "project_id", None))
            snapshot_project = _uuid_or_none(scope.get("project_id"))
            if persisted_project != snapshot_project:
                raise ClipIngestError("ClipIngest project scopeが変更されています")

            current_session_context: dict[str, Any] = {}
            session_project: uuid.UUID | None = None
            if persisted_session is not None:
                repository = ConversationRepository(preflight)
                try:
                    accessible = await repository.user_has_session_access(
                        str(persisted_session), str(actor)
                    )
                    conversation = (
                        await repository.get_session_by_id(
                            str(persisted_session), with_messages=False
                        )
                        if accessible
                        else None
                    )
                except Exception as exc:
                    raise ClipIngestError(
                        "ClipIngest session ACLを確認できません"
                    ) from exc
                if conversation is None:
                    raise ClipIngestError("ClipIngest sessionへのアクセス権がありません")
                session_project = _uuid_or_none(getattr(conversation, "project_id", None))
                if session_project != persisted_project:
                    raise ClipIngestError("ClipIngest session project scopeが変更されています")
                raw_context = getattr(conversation, "context", None)
                if isinstance(raw_context, Mapping):
                    mode = self._privacy_mode(raw_context.get("privacy_mode"))
                    if mode:
                        current_session_context["privacy_mode"] = mode

            current_project_metadata: dict[str, Any] = {}
            if persisted_project is not None:
                try:
                    readable = await ProjectRepository.has_permission(
                        preflight, persisted_project, actor, "read"
                    )
                    project = await ProjectRepository.get_by_id(
                        preflight, persisted_project
                    )
                except Exception as exc:
                    raise ClipIngestError(
                        "ClipIngest project ACLを確認できません"
                    ) from exc
                if not readable or project is None:
                    raise ClipIngestError("ClipIngest projectへのアクセス権がありません")
                raw_metadata = getattr(project, "project_metadata", None)
                if isinstance(raw_metadata, Mapping):
                    mode = self._privacy_mode(raw_metadata.get("privacy_mode"))
                    if mode:
                        current_project_metadata["privacy_mode"] = mode
                current_project_metadata["project_id"] = str(persisted_project)

            library_id = _uuid_or_none(getattr(job, "docs_library_id", None))
            if library_id is None:
                raise ClipIngestError("ClipIngest Docs libraryを確認できません")
            library = await preflight.get(DocsLibrary, library_id)
            if library is None:
                raise ClipIngestError("ClipIngest Docs libraryを確認できません")

            snapshot_target = request.get("target")
            snapshot_target_id = _uuid_or_none(
                snapshot_target.get("node_id")
                if isinstance(snapshot_target, Mapping)
                else None
            )
            target_id = _uuid_or_none(getattr(job, "target_node_id", None))
            if target_id != snapshot_target_id:
                raise ClipIngestError("ClipIngest jobの保存先指定が変更されています")
            if target_id is not None:
                target = await preflight.get(KnowledgeNode, target_id)
                if (
                    target is None
                    or getattr(target, "archived_at", None) is not None
                    or _uuid_or_none(getattr(target, "docs_library_id", None))
                    != library_id
                ):
                    raise ClipIngestError("ClipIngest jobの保存先が利用できません")
                try:
                    writable = await can_write_node(
                        preflight, target, actor, library=library
                    )
                except Exception as exc:
                    raise ClipIngestError(
                        "ClipIngest jobの保存先権限を確認できません"
                    ) from exc
                if not writable:
                    raise ClipIngestError("ClipIngest jobの保存先書き込み権限がありません")
            else:
                # Automatic classification must still have a writable current
                # library; target selection later cannot widen this scope. A
                # project-bound job derives write authority from its current
                # project ACL, while a personal job requires library ownership.
                try:
                    writable = (
                        await ProjectRepository.has_permission(
                            preflight, persisted_project, actor, "write"
                        )
                        if persisted_project is not None
                        else await library_can_write(preflight, library, actor)
                    )
                except Exception as exc:
                    raise ClipIngestError(
                        "ClipIngest Docs library権限を確認できません"
                    ) from exc
                if not writable:
                    raise ClipIngestError("ClipIngest Docs library書き込み権限がありません")

            snapshot_session_mode = self._privacy_mode(
                session_context.get("privacy_mode")
                if isinstance(session_context, Mapping)
                else None
            )
            snapshot_project_mode = self._privacy_mode(
                project_metadata.get("privacy_mode")
                if isinstance(project_metadata, Mapping)
                else None
            )
            session_mode = self._privacy_mode(
                current_session_context.get("privacy_mode")
            )
            project_mode = self._privacy_mode(
                current_project_metadata.get("privacy_mode")
            )
            effective_session = self._stricter_privacy(
                snapshot_session_mode, session_mode
            )
            effective_project = self._stricter_privacy(
                snapshot_project_mode, project_mode
            )
            effective = self._stricter_privacy(effective_session, effective_project)
            if effective:
                normalized_scope = dict(scope)
                normalized_scope["user_id"] = str(actor)
                normalized_scope["session_id"] = (
                    str(persisted_session) if persisted_session is not None else None
                )
                normalized_scope["project_id"] = (
                    str(persisted_project) if persisted_project is not None else None
                )
                normalized_scope["privacy_mode"] = effective
            else:
                normalized_scope = dict(scope)
                normalized_scope["user_id"] = str(actor)
                normalized_scope["session_id"] = (
                    str(persisted_session) if persisted_session is not None else None
                )
                normalized_scope["project_id"] = (
                    str(persisted_project) if persisted_project is not None else None
                )
            return (
                normalized_scope,
                {"privacy_mode": effective_session} if effective_session else None,
                {
                    "project_id": str(persisted_project),
                    "privacy_mode": effective_project,
                }
                if persisted_project is not None and effective_project
                else None,
            )
        finally:
            try:
                await preflight.rollback()
            except Exception:
                pass
            await preflight.close()

    @staticmethod
    def _privacy_mode(value: Any) -> str | None:
        normalized = str(value or "").strip().lower()
        return normalized if normalized in {"direct", "protected", "local_only"} else None

    @classmethod
    def _stricter_privacy(cls, *values: Any) -> str | None:
        rank = {"direct": 0, "protected": 1, "local_only": 2}
        valid = [mode for mode in (cls._privacy_mode(value) for value in values) if mode]
        return max(valid, key=lambda mode: rank[mode]) if valid else None

    async def _with_plan_llm(
        self,
        *,
        user_id: Any,
        scope: Mapping[str, Any],
        session_context: Mapping[str, Any] | None = None,
        project_metadata: Mapping[str, Any] | None = None,
        callback,
    ):
        async def invoke(llm: Any):
            # The factory's context object is the only authority for the
            # request-scoped ClipIngest media route/client.  Keep the route
            # JSON-compatible and pass the client as an ephemeral live object;
            # never attempt to deserialize a client from ``request_json``.
            values = {
                "clip_ingest_route": _safe_clip_ingest_route(
                    getattr(llm, "clip_ingest_route", None)
                ),
                "clip_ingest_client": getattr(llm, "clip_ingest_client", None),
            }
            kwargs = _supported_kwargs(callback, values)
            result = callback(llm, **kwargs)
            return await result if inspect.isawaitable(result) else result

        if self.plan_llm_factory is not None:
            privacy_mode = scope.get("privacy_mode")
            if privacy_mode is None and isinstance(session_context, Mapping):
                privacy_mode = session_context.get("privacy_mode")
            if privacy_mode is None and isinstance(project_metadata, Mapping):
                privacy_mode = project_metadata.get("privacy_mode")
            values = {
                "user_id": str(user_id),
                "session_id": _string_or_none(scope.get("session_id")),
                "project_id": _string_or_none(scope.get("project_id")),
                "scope": dict(scope),
                "privacy": scope.get("privacy") or scope.get("privacy_mode"),
                "privacy_mode": _string_or_none(privacy_mode),
                "session_context": dict(session_context) if session_context else None,
                "project_metadata": dict(project_metadata) if project_metadata else None,
            }
            kwargs = _supported_kwargs(self.plan_llm_factory, values)
            context = self.plan_llm_factory(**kwargs)
            if inspect.isawaitable(context):
                context = await context
            async with context as llm:
                return await invoke(llm)
        if self.plan_llm is not None:
            return await invoke(self.plan_llm)
        raise DocsIngestUnavailableError("clip ingest LLM is not configured")

    @staticmethod
    def _result_snapshot(result: Any) -> dict[str, Any]:
        """Return a JSON-safe public result, never private rollback handles."""

        if dataclasses.is_dataclass(result):
            raw = {
                field.name: getattr(result, field.name, None)
                for field in dataclasses.fields(result)
            }
        elif isinstance(result, Mapping):
            raw = dict(result)
        else:
            raw = dict(getattr(result, "__dict__", {}) or {})
        # Receipt linkage is attached dynamically by DocsIngestService.
        for key in ("clip_ingest_receipt_id", "receipt_id"):
            if key not in raw and hasattr(result, key):
                raw[key] = getattr(result, key)
        return _json_safe(raw)

    @staticmethod
    def _safe_error(exc: Exception) -> tuple[dict[str, Any], bool]:
        if isinstance(exc, DocsIngestUnavailableError):
            code = str(getattr(exc, "safe_code", "unknown") or "unknown")[:64]
            message = str(
                getattr(exc, "safe_message", "LLM unavailable") or "LLM unavailable"
            )[:500]
            retryable = bool(getattr(exc, "retryable", True))
            return {
                "category": "llm_unavailable",
                "code": code,
                "message": message,
                "retryable": retryable,
            }, retryable
        if isinstance(exc, ClipIngestError):
            return {
                "category": "validation",
                "code": "clip_ingest_error",
                "message": str(exc)[:500],
                "retryable": False,
            }, False
        return {
            "category": "internal",
            "code": "worker_error",
            "message": "クリップ取り込みを完了できませんでした",
            "retryable": True,
        }, True

    async def _mark_failed(self, job_id: uuid.UUID, token: str, exc: Exception) -> None:
        payload, retryable = self._safe_error(exc)
        session = None
        try:
            session = await self._new_session()
            now = datetime.utcnow()
            await session.execute(
                update(DocsClipIngestJob)
                .where(
                    DocsClipIngestJob.id == job_id,
                    DocsClipIngestJob.status == "running",
                    DocsClipIngestJob.lease_token == token,
                    DocsClipIngestJob.lease_expires_at > now,
                )
                .values(
                    status="failed",
                    error_json=payload,
                    retryable=retryable,
                    finished_at=now,
                    updated_at=now,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                )
            )
            await session.commit()
        except Exception:
            if session is not None:
                await session.rollback()
            logger.exception("Failed to persist Docs ClipIngest error: %s", job_id)
        finally:
            if session is not None:
                await session.close()

    async def _cleanup_side_effects(
        self,
        result: Any,
        service: Any,
        *,
        user_id: Any,
        upload_ids: list[Any],
        job_id: uuid.UUID | None = None,
        token: str | None = None,
        commit_ambiguous: bool = False,
    ) -> None:
        """Best-effort rollback for private promotion handles only."""

        if self._retain_side_effects_on_failure:
            # Durable jobs may be retried after a provider failure, lease
            # fencing, or an ambiguous status transition.  Removing a
            # deterministic promoted file here can destroy the only payload
            # a retry can recover.  The worker's staging GC is deliberately
            # post-commit-only; leave every promotion handle intact on all
            # failure paths, including before/after ``_mark_failed``.
            return

        if job_id is not None and token is not None:
            if not await self._lease_allows_cleanup(
                job_id,
                token,
                commit_ambiguous=commit_ambiguous,
            ):
                # A replacement worker may own the same upload handles.  Keep
                # files on lease loss or any DB uncertainty; a later retry can
                # safely verify/reuse deterministic promoted files.
                return

        storage = getattr(result, "_clip_ingest_storage", None)
        if storage is None:
            storage = getattr(service, "_attachment_storage", None)
        paths = list(getattr(result, "_clip_ingest_promoted_paths", []) or [])
        if not paths and service is not None:
            paths = list(getattr(service, "_promoted_paths", []) or [])
        promoted_upload_ids = list(
            getattr(result, "_clip_ingest_upload_ids", []) or []
        )
        if not promoted_upload_ids and service is not None:
            promoted_upload_ids = list(
                getattr(service, "_attachment_upload_ids", []) or []
            )
        if storage is None:
            return
        # Durable worker promotion deliberately retains destination files and
        # staging until the DB commit is known to have succeeded.  On any
        # preparation/finalization/commit-ambiguity failure, retain both so a
        # retry can verify and reuse the deterministic payload.
        if bool(getattr(storage, "defer_staging_cleanup", False)):
            return
        try:
            cleanup_promoted = getattr(storage, "cleanup_promoted", None)
            if paths and callable(cleanup_promoted):
                cleanup_promoted(paths)
            cleanup_uploads = getattr(storage, "cleanup_uploads", None)
            ids = promoted_upload_ids or list(upload_ids or [])
            if ids and callable(cleanup_uploads):
                value = cleanup_uploads(user_id, ids)
                if inspect.isawaitable(value):
                    await value
        except Exception:
            logger.exception("Failed to cleanup Docs ClipIngest side effects")

    async def _cleanup_staging_after_commit(
        self,
        result: Any,
        service: Any,
        *,
        user_id: Any,
        upload_ids: list[Any],
    ) -> None:
        """Delete worker staging only after the job transaction commits."""

        storage = getattr(result, "_clip_ingest_storage", None)
        if storage is None:
            storage = getattr(service, "storage", None)
        if storage is None:
            storage = self.storage
        cleanup_uploads = getattr(storage, "cleanup_uploads", None)
        if not callable(cleanup_uploads):
            return
        ids = list(getattr(result, "_clip_ingest_upload_ids", []) or [])
        if not ids and service is not None:
            ids = list(getattr(service, "_attachment_upload_ids", []) or [])
        if not ids:
            ids = list(upload_ids or [])
        if not ids:
            return
        try:
            value = cleanup_uploads(user_id, ids)
            if inspect.isawaitable(value):
                await value
        except Exception:
            logger.exception("Failed to cleanup committed Docs ClipIngest staging")

    async def _lease_allows_cleanup(
        self,
        job_id: uuid.UUID,
        token: str,
        *,
        commit_ambiguous: bool = False,
    ) -> bool:
        """Return true only while this worker still owns an unexpired lease."""

        session = None
        try:
            session = await self._new_session()
            row = await session.scalar(
                select(DocsClipIngestJob).where(
                    DocsClipIngestJob.id == job_id,
                    DocsClipIngestJob.status == "running",
                    DocsClipIngestJob.lease_token == token,
                    DocsClipIngestJob.lease_expires_at > datetime.utcnow(),
                )
            )
            await session.rollback()
            if row is None:
                return False
            if getattr(row, "receipt_id", None) is not None:
                return False
            if commit_ambiguous:
                return False
            return True
        except Exception:
            if session is not None:
                try:
                    await session.rollback()
                except Exception:
                    pass
            return False
        finally:
            if session is not None:
                await session.close()


def _supported_kwargs(factory: Callable[..., Any], values: dict[str, Any]) -> dict[str, Any]:
    try:
        parameters = inspect.signature(factory).parameters
    except (TypeError, ValueError):
        return values
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return values
    return {
        key: value
        for key, value in values.items()
        if key in parameters
        and parameters[key].kind
        in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
    }


def _safe_clip_ingest_route(value: Any) -> dict[str, Any] | None:
    """Return allowlisted, JSON-compatible ClipIngest route metadata.

    A context-managed plan LLM may expose arbitrary runtime attributes.  Only
    the scalar route fields consumed by the Docs/media services cross the
    worker callback boundary; nested clients, storage handles, and custom
    objects are intentionally dropped.  Credentials are never copied from the
    context route: the factory has already resolved them into the live client.
    """

    if not isinstance(value, Mapping):
        return None
    safe: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        if key not in _SAFE_CLIP_INGEST_ROUTE_KEYS:
            continue
        if raw_value is None:
            safe[key] = None
        elif isinstance(raw_value, (bool, int, str)):
            safe[key] = raw_value
        elif isinstance(raw_value, float) and math.isfinite(raw_value):
            safe[key] = raw_value
    return safe or None


def _string_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _uuid_or_none(value: Any):
    if value in (None, ""):
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return None
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (uuid.UUID, datetime)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if isinstance(value, Path):
        # Absolute host paths are never result data.
        return None
    if dataclasses.is_dataclass(value):
        return _json_safe(
            {
                field.name: getattr(value, field.name, None)
                for field in dataclasses.fields(value)
            },
            depth=depth + 1,
        )
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name.startswith("_") or name in {"receipt", "storage"}:
                continue
            safe = _json_safe(item, depth=depth + 1)
            if safe is not None or item is None:
                output[name] = safe
        return output
    if isinstance(value, (list, tuple, set)):
        output = []
        for item in list(value)[:1000]:
            safe = _json_safe(item, depth=depth + 1)
            if safe is not None:
                output.append(safe)
        return output
    if hasattr(value, "__dict__"):
        return _json_safe(
            {
                key: item
                for key, item in vars(value).items()
                if not key.startswith("_") and key not in {"receipt", "storage"}
            },
            depth=depth + 1,
        )
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return None
    return value


__all__ = ["DocsClipIngestWorker"]
