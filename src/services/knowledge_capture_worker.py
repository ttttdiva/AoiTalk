"""Durable orchestration worker for Resolution Knowledge Capture.

This module is deliberately a thin execution boundary.  Candidate state
transitions remain in ``knowledge_capture_candidate_service`` and evidence /
LLM/research remains in ``knowledge_capture_research_service``.  The worker
only performs bounded recovery, claim, public research invocation, and final
state handoff across short database sessions.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import socket
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from .knowledge_capture_contract import evidence_digest

logger = logging.getLogger(__name__)

DEFAULT_KNOWLEDGE_CAPTURE_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_KNOWLEDGE_CAPTURE_RECOVERY_SCAN_LIMIT = 25
DEFAULT_KNOWLEDGE_CAPTURE_RECOVERY_LOOKBACK_SECONDS = 300.0
MAX_KNOWLEDGE_CAPTURE_RECOVERY_LOOKBACK_SECONDS = 86400.0
DEFAULT_KNOWLEDGE_CAPTURE_LEASE_SECONDS = 300.0


def _bounded_positive_float(value: Any, default: float, minimum: float) -> float:
    try:
        return max(minimum, float(value))
    except (TypeError, ValueError):
        return default


def _bounded_positive_int(value: Any, default: int, maximum: int) -> int:
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def _load_candidate_service() -> Any | None:
    try:
        from . import knowledge_capture_candidate_service

        return knowledge_capture_candidate_service
    except (ImportError, ModuleNotFoundError):
        return None


def _load_research_service() -> Any | None:
    try:
        from . import knowledge_capture_research_service

        return knowledge_capture_research_service
    except (ImportError, ModuleNotFoundError):
        return None


def _target(module: Any, names: tuple[str, ...]) -> Any | None:
    if module is None:
        return None
    for name in names:
        value = getattr(module, name, None)
        if callable(value):
            return value
    for class_name in (
        "KnowledgeCaptureCandidateService",
        "KnowledgeCaptureService",
        "KnowledgeCaptureResearchService",
    ):
        cls = getattr(module, class_name, None)
        if not callable(cls):
            continue
        try:
            instance = cls()
        except TypeError:
            try:
                instance = cls(config=None)
            except TypeError:
                continue
        for name in names:
            value = getattr(instance, name, None)
            if callable(value):
                return value
    return None


async def _invoke(target: Any, kwargs: dict[str, Any]) -> Any:
    if target is None:
        return None
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        signature = None
    if signature is not None and not any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    ):
        kwargs = {
            key: value for key, value in kwargs.items() if key in signature.parameters
        }
    result = target(**kwargs)
    return await result if inspect.isawaitable(result) else result


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _outcome_status(value: Any) -> str:
    return str(_field(value, "status", "") or "").strip().casefold()


def _bounded_worker_text(value: Any, limit: int, fallback: str) -> str:
    text = str(value or "").replace("\x00", "").strip()
    return (text or fallback)[:limit]


def _recovery_count(value: Any) -> int:
    if isinstance(value, Mapping):
        return int(
            value.get("created")
            or 0
        ) + int(value.get("enqueued") or 0) + int(value.get("recovered") or 0) + int(
            value.get("failed") or 0
        )
    return int(value or 0)


class KnowledgeCaptureWorker:
    """Poll, recover, claim, and hand off Knowledge Capture candidates."""

    def __init__(
        self,
        db_manager: Any | None = None,
        *,
        config: Any = None,
        poll_interval_seconds: float | None = None,
        recovery_scan_limit: int | None = None,
        recovery_lookback_seconds: float | None = None,
        lease_seconds: float | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.db_manager = db_manager
        self.config = config
        configured_poll = poll_interval_seconds
        if configured_poll is None:
            configured_poll = os.getenv(
                "AOITALK_KNOWLEDGE_CAPTURE_POLL_INTERVAL_SECONDS",
                str(DEFAULT_KNOWLEDGE_CAPTURE_POLL_INTERVAL_SECONDS),
            )
        configured_limit = recovery_scan_limit
        if configured_limit is None:
            configured_limit = os.getenv(
                "AOITALK_KNOWLEDGE_CAPTURE_RECOVERY_SCAN_LIMIT",
                str(DEFAULT_KNOWLEDGE_CAPTURE_RECOVERY_SCAN_LIMIT),
            )
        configured_lookback = recovery_lookback_seconds
        if configured_lookback is None:
            configured_lookback = os.getenv(
                "AOITALK_KNOWLEDGE_CAPTURE_RECOVERY_LOOKBACK_SECONDS",
                str(DEFAULT_KNOWLEDGE_CAPTURE_RECOVERY_LOOKBACK_SECONDS),
            )
        configured_lease = lease_seconds
        if configured_lease is None:
            configured_lease = os.getenv(
                "AOITALK_KNOWLEDGE_CAPTURE_LEASE_SECONDS",
                str(DEFAULT_KNOWLEDGE_CAPTURE_LEASE_SECONDS),
            )
        self.poll_interval_seconds = _bounded_positive_float(
            configured_poll,
            DEFAULT_KNOWLEDGE_CAPTURE_POLL_INTERVAL_SECONDS,
            0.1,
        )
        self.recovery_scan_limit = _bounded_positive_int(
            configured_limit,
            DEFAULT_KNOWLEDGE_CAPTURE_RECOVERY_SCAN_LIMIT,
            200,
        )
        self.recovery_lookback_seconds = min(
            MAX_KNOWLEDGE_CAPTURE_RECOVERY_LOOKBACK_SECONDS,
            _bounded_positive_float(
                configured_lookback,
                DEFAULT_KNOWLEDGE_CAPTURE_RECOVERY_LOOKBACK_SECONDS,
                30.0,
            ),
        )
        # Periodic recovery is a rollout/restart safety net, not a historical
        # backfill.  Keep one fixed lower bound for this worker lifetime so a
        # temporarily unhealthy recovery path does not age recent completions
        # out of eligibility.
        self.recovery_completed_after = datetime.utcnow() - timedelta(
            seconds=self.recovery_lookback_seconds
        )
        self.lease_seconds = _bounded_positive_float(
            configured_lease,
            DEFAULT_KNOWLEDGE_CAPTURE_LEASE_SECONDS,
            30.0,
        )
        self.worker_id = worker_id or (
            f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        )
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[Any] | None = None
        self._running = False

    @property
    def running(self) -> bool:
        return bool(self._running and self._task and not self._task.done())

    @property
    def task(self) -> asyncio.Task[Any] | None:
        return self._task

    async def _new_session(self) -> Any:
        manager = self.db_manager
        if manager is None or not callable(getattr(manager, "get_session", None)):
            raise RuntimeError("Knowledge Capture database manager is unavailable")
        result = manager.get_session()
        return await result if inspect.isawaitable(result) else result

    @staticmethod
    async def _close_session(session: Any) -> None:
        if session is None:
            return
        close = getattr(session, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

    async def _invoke_recovery_target(
        self,
        target: Any,
        *,
        completed_after: datetime | None = None,
    ) -> int:
        session = await self._new_session()
        try:
            kwargs = {
                "session": session,
                "limit": self.recovery_scan_limit,
                "scan_limit": self.recovery_scan_limit,
                "max_rows": self.recovery_scan_limit,
                "worker_id": self.worker_id,
            }
            if completed_after is not None:
                kwargs["completed_after"] = completed_after
            result = await _invoke(
                target,
                kwargs,
            )
            commit = getattr(session, "commit", None)
            if callable(commit):
                await commit()
            return _recovery_count(result)
        except Exception:
            rollback = getattr(session, "rollback", None)
            if callable(rollback):
                await rollback()
            raise
        finally:
            await self._close_session(session)

    async def _recovery_scan(self) -> int:
        module = _load_candidate_service()
        expired_target = _target(
            module,
            (
                "recover_expired_candidate_leases",
                "recover_stale_leases",
                "recover_leases",
            ),
        )
        missing_target = _target(
            module,
            ("recover_missing_candidates", "recovery_scan", "scan_recovery"),
        )
        if expired_target is None and missing_target is None:
            return 0
        recovered = 0
        # Expired leases must be committed in their own short session before
        # missing-candidate enqueue begins.  A later missing-scan exception
        # must not roll the expired recovery back.
        if expired_target is not None:
            recovered += await self._invoke_recovery_target(expired_target)
        if missing_target is not None:
            try:
                recovered += await self._invoke_recovery_target(
                    missing_target,
                    completed_after=self.recovery_completed_after,
                )
            except Exception as exc:
                logger.warning(
                    "Knowledge Capture missing-candidate recovery failed: exception_type=%s",
                    type(exc).__name__,
                )
                raise
        return recovered

    async def _claim_one(self) -> Any | None:
        module = _load_candidate_service()
        target = _target(
            module,
            ("claim_next_candidate", "claim_candidate", "claim_one"),
        )
        if target is None:
            return None
        session = await self._new_session()
        try:
            result = await _invoke(
                target,
                {
                    "session": session,
                    "owner": self.worker_id,
                    "claim_owner": self.worker_id,
                    "worker_id": self.worker_id,
                    "lease_seconds": self.lease_seconds,
                    "limit": 1,
                },
            )
            commit = getattr(session, "commit", None)
            if callable(commit):
                await commit()
            # Do not carry an ORM instance across the short claim session.
            # Most SQLAlchemy sessions expire rows on commit, and handing the
            # detached object to the research service would make a worker
            # restart look like a research failure.  Keep only the bounded
            # workflow projection plus the lease fence.
            serializer = getattr(result, "to_safe_dict", None)
            if callable(serializer):
                payload = dict(serializer(include_body=True))
                payload["lease_token"] = _field(result, "lease_token")
                payload["lease_owner"] = _field(result, "lease_owner")
                return payload
            return result
        except Exception:
            rollback = getattr(session, "rollback", None)
            if callable(rollback):
                await rollback()
            raise
        finally:
            await self._close_session(session)

    async def _research(self, claim: Any) -> Any:
        # Project policy is live state, not the enqueue snapshot.  A mode
        # change to off must stop queued work before any LLM/web call.
        project_id = _field(claim, "project_id")
        if project_id is not None:
            policy_session = await self._new_session()
            try:
                from .knowledge_capture_candidate_service import (
                    get_project_knowledge_capture_mode,
                )

                if await get_project_knowledge_capture_mode(policy_session, project_id) == "off":
                    return {
                        "status": "discarded",
                        "project_id": str(project_id),
                        "candidate_id": str(_field(claim, "id")),
                        "candidate_version": _field(claim, "version"),
                        "reason": "Project Knowledge Capture mode is off",
                        "evidence": [],
                    }
            finally:
                await self._close_session(policy_session)
        module = _load_research_service()
        target = _target(
            module,
            ("research_candidate", "process_candidate", "run_candidate_research"),
        )
        if target is None:
            # A research implementation may live beside the candidate service
            # during a rolling deployment.  It still receives the same narrow
            # public call and is never implemented by this worker.
            target = _target(
                _load_candidate_service(),
                ("research_candidate", "process_candidate"),
            )
        if target is None:
            raise RuntimeError("Knowledge Capture research service unavailable")
        return await _invoke(
            target,
            {
                "candidate": claim,
                "claim": claim,
                "session_factory": self._new_session,
                "config": self.config,
                "worker_id": self.worker_id,
            },
        )

    async def _load_candidate_row(self, session: Any, candidate_id: Any) -> Any:
        from ..memory.models import KnowledgeCaptureCandidate

        result = await session.execute(
            select(KnowledgeCaptureCandidate)
            .where(KnowledgeCaptureCandidate.id == candidate_id)
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def _recipient_for_candidate(self, session: Any, candidate: Any) -> Any:
        recipient = _field(candidate, "trigger_user_id")
        if recipient is not None:
            return recipient
        from ..memory.models import Project

        result = await session.execute(
            select(Project.owner_id).where(
                Project.id == _field(candidate, "project_id"),
                Project.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def _persist_capture_outcome(self, claim: Any, outcome: Any) -> Any:
        """Apply a validated outcome and emit only typed capture notifications.

        This is workflow glue, not research logic.  If a later candidate
        service exposes one atomic settle method, ``_finish`` prefers that
        public method and this compatibility path becomes unused.
        """

        candidate_id = _field(outcome, "candidate_id") or _field(claim, "id")
        if candidate_id is None:
            raise RuntimeError("Knowledge Capture outcome has no candidate id")
        candidate_module = _load_candidate_service()
        status = _outcome_status(outcome)
        session = await self._new_session()
        try:
            candidate = await self._load_candidate_row(session, candidate_id)
            if candidate is None:
                raise RuntimeError("Knowledge Capture candidate disappeared")
            evidence_rows = _field(outcome, "evidence", ()) or ()
            if isinstance(evidence_rows, (list, tuple)):
                compact_refs = []
                for item in list(evidence_rows)[:160]:
                    if not isinstance(item, Mapping):
                        continue
                    evidence_id = str(item.get("id") or item.get("source_id") or "").strip()
                    kind = str(item.get("kind") or item.get("type") or "").strip()
                    if not evidence_id or not kind:
                        continue
                    ref = {"type": kind[:64], "id": evidence_id[:240]}
                    if item.get("content_hash"):
                        ref["content_hash"] = str(item["content_hash"])[:96]
                    if item.get("version"):
                        ref["version"] = str(item["version"])[:80]
                    if item.get("source_path"):
                        ref["source_path"] = str(item["source_path"])[:500]
                    compact_refs.append(ref)
                if compact_refs:
                    candidate.evidence_refs = compact_refs
                    candidate.evidence_digest = str(
                        _field(outcome, "evidence_digest")
                        or _field(outcome, "digest")
                        or evidence_digest(evidence_rows)
                    )[:64] or None
            research_payload = _field(outcome, "research")
            if research_payload is not None:
                candidate.research_json = research_payload
            expected_version = _field(outcome, "candidate_version")
            if expected_version is None:
                expected_version = _field(candidate, "version")
            actor_id = _field(candidate, "trigger_user_id")

            if status == "needs_user":
                question = _field(outcome, "question", {}) or {}
                question = (
                    question.to_dict()
                    if callable(getattr(question, "to_dict", None))
                    else question
                )
                question_text = _bounded_worker_text(
                    _field(question, "message")
                    or _field(question, "question")
                    or _field(question, "text"),
                    2000,
                    "最終的な解決内容を確認してください。",
                )
                create_question = _target(
                    candidate_module,
                    ("create_question", "ask_question"),
                )
                if create_question is None:
                    raise RuntimeError("Knowledge Capture question service unavailable")
                created_question = await _invoke(
                    create_question,
                    {
                        "session": session,
                        "candidate_id": candidate_id,
                        "question": question_text,
                        "title": _bounded_worker_text(
                            _field(question, "title"),
                            240,
                            "確認が必要です",
                        ),
                        "message": question_text,
                        "options_json": _field(question, "options", []),
                        "evidence_digest": _field(outcome, "evidence_digest")
                        or _field(candidate, "evidence_digest"),
                        "round_number": _field(question, "round_number"),
                        "asked_by_user_id": actor_id,
                        "expected_candidate_version": expected_version,
                    },
                )
                candidate = await self._load_candidate_row(session, candidate_id)
                question_id = _field(created_question, "id")
                recipient_id = await self._recipient_for_candidate(session, candidate)
                if recipient_id is not None and question_id is not None:
                    from .task_management.notifications import (
                        persist_knowledge_capture_notification,
                    )

                    await persist_knowledge_capture_notification(
                        session,
                        project_id=candidate.project_id,
                        recipient_user_id=recipient_id,
                        candidate_id=candidate.id,
                        question_id=question_id,
                        candidate_version=int(candidate.version or 1),
                        notification_type="knowledge_capture_question",
                        title="ナレッジ確認",
                        message=question_text,
                    )
            elif status == "draft_ready":
                transition = _target(candidate_module, ("transition_candidate",))
                edit_candidate = _target(candidate_module, ("edit_candidate",))
                draft = _field(outcome, "draft")
                if transition is None or edit_candidate is None or not isinstance(
                    draft, Mapping
                ):
                    raise RuntimeError("Knowledge Capture draft service unavailable")
                transitioned = await _invoke(
                    transition,
                    {
                        "session": session,
                        "candidate_id": candidate_id,
                        "target_status": "draft_ready",
                        "expected_version": expected_version,
                        "knowledge_semantic_key": _field(draft, "semantic_key"),
                    },
                )
                await _invoke(
                    edit_candidate,
                    {
                        "session": session,
                        "candidate_id": candidate_id,
                        "draft_json": dict(draft),
                        "expected_version": _field(transitioned, "version"),
                        "mark_user_edited": False,
                    },
                )
                candidate = await self._load_candidate_row(session, candidate_id)
                recipient_id = await self._recipient_for_candidate(session, candidate)
                auto_published = False
                try:
                    from ..memory.models import ProjectKnowledgeCaptureSetting

                    setting = await session.scalar(
                        select(ProjectKnowledgeCaptureSetting).where(
                            ProjectKnowledgeCaptureSetting.project_id == candidate.project_id
                        )
                    )
                    current_mode = str(getattr(setting, "mode", "suggest") or "suggest").casefold()
                    score = _field(outcome, "reuse_score")
                    confidence = _field(outcome, "confidence")
                    from .knowledge_capture_contract import is_authoritative_local_success_item

                    authoritative = any(
                        isinstance(item, Mapping) and is_authoritative_local_success_item(item)
                        for item in evidence_rows
                    )
                    if (
                        current_mode == "auto"
                        and recipient_id is not None
                        and score is not None
                        and confidence is not None
                        and int(score) >= 80
                        and float(confidence) >= 0.90
                        and authoritative
                        and not bool(getattr(candidate, "user_edited", False))
                    ):
                        from .knowledge_capture_publisher import publish_candidate

                        # Keep a savepoint around the optional automatic side
                        # effect. Any ACL/revision/evidence failure falls back
                        # to the normal suggestion state without losing the
                        # validated draft or leaving half a Docs subtree.
                        async with session.begin_nested():
                            await publish_candidate(
                                session,
                                candidate_id=candidate.id,
                                user_id=recipient_id,
                                expected_version=int(candidate.version or 1),
                            )
                        auto_published = True
                except Exception:
                    logger.debug(
                        "Knowledge Capture strict auto-publish fell back to suggestion",
                        exc_info=True,
                    )
                if recipient_id is not None and not auto_published:
                    from .task_management.notifications import (
                        persist_knowledge_capture_notification,
                    )

                    await persist_knowledge_capture_notification(
                        session,
                        project_id=candidate.project_id,
                        recipient_user_id=recipient_id,
                        candidate_id=candidate.id,
                        candidate_version=int(candidate.version or 1),
                        notification_type="knowledge_capture_draft",
                        title="ナレッジ候補",
                        message=_bounded_worker_text(
                            _field(draft, "title"),
                            1000,
                            "再利用可能な解決手順の候補があります。",
                        ),
                    )
            elif status == "retryable":
                retry = _target(candidate_module, ("retry_candidate",))
                lease_token = _field(claim, "lease_token")
                if retry is not None and lease_token:
                    await _invoke(
                        retry,
                        {
                            "session": session,
                            "candidate_id": candidate_id,
                            "worker_id": self.worker_id,
                            "lease_token": lease_token,
                            "expected_version": expected_version,
                            "error_code": "capture_retryable_error",
                            "error_message": _bounded_worker_text(
                                _field(outcome, "reason"),
                                512,
                                "capture retryable error",
                            ),
                        },
                    )
            elif status in {"discarded", "low_value"}:
                transition = _target(candidate_module, ("transition_candidate",))
                if transition is None:
                    raise RuntimeError("Knowledge Capture discard service unavailable")
                await _invoke(
                    transition,
                    {
                        "session": session,
                        "candidate_id": candidate_id,
                        "target_status": "discarded",
                        "expected_version": expected_version,
                        "lease_owner": self.worker_id,
                        "lease_token": _field(claim, "lease_token"),
                        "error_code": "low_reuse_value",
                        "error_message": _bounded_worker_text(
                            _field(outcome, "reason"),
                            512,
                            "resolution is not reusable",
                        ),
                    },
                )
            # Low-value/discarded outcomes intentionally do not create a
            # notification.  Candidate state remains owned by the domain
            # service until it exposes an explicit discard transition.
            commit = getattr(session, "commit", None)
            if callable(commit):
                await commit()
            return outcome
        except Exception:
            rollback = getattr(session, "rollback", None)
            if callable(rollback):
                await rollback()
            raise
        finally:
            await self._close_session(session)

    async def _finish(self, claim: Any, result: Any) -> Any:
        module = _load_candidate_service()
        target = _target(
            module,
            (
                "apply_research_result",
                "complete_candidate",
                "settle_candidate",
                "record_research_result",
                "finish_candidate",
            ),
        )
        if target is None:
            return await self._persist_capture_outcome(claim, result)
        session = await self._new_session()
        try:
            finished = await _invoke(
                target,
                {
                    "session": session,
                    "candidate": claim,
                    "claim": claim,
                    "result": result,
                    "research_result": result,
                    "worker_id": self.worker_id,
                },
            )
            commit = getattr(session, "commit", None)
            if callable(commit):
                await commit()
            return finished
        except Exception:
            rollback = getattr(session, "rollback", None)
            if callable(rollback):
                await rollback()
            raise
        finally:
            await self._close_session(session)

    async def run_once(self) -> dict[str, int]:
        """Run one bounded recovery + candidate claim tick."""

        stats = {"recovered": 0, "claimed": 0, "processed": 0, "failed": 0}
        try:
            stats["recovered"] = await self._recovery_scan()
        except Exception as exc:
            logger.warning(
                "Knowledge Capture recovery scan failed: exception_type=%s",
                type(exc).__name__,
            )

        try:
            claim = await self._claim_one()
        except Exception as exc:
            logger.warning(
                "Knowledge Capture claim failed: exception_type=%s",
                type(exc).__name__,
            )
            stats["failed"] += 1
            return stats
        if claim is None:
            return stats
        stats["claimed"] = 1
        try:
            result = await self._research(claim)
            await self._finish(claim, result)
            stats["processed"] = 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            stats["failed"] += 1
            logger.warning(
                "Knowledge Capture candidate processing failed: exception_type=%s",
                type(exc).__name__,
            )
        return stats

    async def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._running = True
        self._task = asyncio.create_task(
            self._run_loop(),
            name="aoitalk-knowledge-capture-worker",
        )

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self) -> None:
        while self._running and not self._stop_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Knowledge Capture worker tick failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.poll_interval_seconds
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise


__all__ = ["KnowledgeCaptureWorker"]
