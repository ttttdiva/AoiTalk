"""
Heartbeatシステム - ランナー

HeartbeatRunStateをscheduler source of truthとして定期実行する。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Coroutine, Dict, List, Mapping, Optional

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from ..llm.generation_error import (
    GenerationErrorKind,
    classify_generation_error,
)
from ..memory.models import HeartbeatRunState, Project

# Heartbeat run history is an optional companion to the scheduler state.  The
# history model/repository is loaded lazily so lightweight test databases and
# older deployments which have not applied its migration can continue to run
# the scheduler without making logging a correctness dependency.
try:  # pragma: no cover - exercised when the history migration is installed
    from .history import HeartbeatRunHistoryRepository, project_result as _project_history_result
except Exception:  # pragma: no cover - compatibility with pre-history trees
    HeartbeatRunHistoryRepository = None  # type: ignore[assignment,misc]
    _project_history_result = None  # type: ignore[assignment]
from .models import HeartbeatDefinition
from .registry import get_heartbeat_registry

logger = logging.getLogger(__name__)

HEARTBEAT_OK = "HEARTBEAT_OK"
HEARTBEAT_RETRY_DELAY = timedelta(minutes=10)
HEARTBEAT_RETRY_JITTER_SECONDS = 120
HEARTBEAT_STALE_AFTER = timedelta(minutes=15)

# Project Steward execution is intentionally single-flight across scopes and
# runner instances on the canonical heartbeat event loop.  The callback may
# share provider/session resources that do not support concurrent requests, and
# startup catch-up should not create an error storm by launching every project
# at once.
_project_steward_execution_lock = asyncio.Lock()

ERROR_EXECUTOR_UNAVAILABLE = "executor_unavailable"
ERROR_EXECUTION_FAILED = "execution_failed"
ERROR_EXECUTION_TIMEOUT = "execution_timeout"
ERROR_STALE_RUNNING = "stale_running_recovered"

_CURSOR_UNSET = object()


class HeartbeatRunner:
    """Heartbeatのdurableバックグラウンド実行を管理する。"""

    def __init__(
        self,
        *,
        session_factory: Optional[Callable[[], Any]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        privacy_config: Any | None = None,
        history_repository: Any | None = None,
        execute_callback: Optional[
            Callable[
                [HeartbeatDefinition, Dict[str, Any]],
                Awaitable[Mapping[str, Any]],
            ]
        ] = None,
    ):
        self._task: Optional[asyncio.Task[Any]] = None
        self._running = False

        # WebChatServerからの既存注入API互換のため保持する。
        # Heartbeat execution自体はこのclientを直接使用しない。
        self._llm_client = None

        self._broadcast_fn: Optional[Callable[[Dict[str, Any]], Coroutine]] = None
        self._admin_notify_fn: Optional[Callable[[Dict[str, Any]], Coroutine]] = None
        self._last_results: Dict[str, Dict[str, Any]] = {}

        self._session_factory = session_factory
        self._clock = clock or datetime.utcnow
        # The scheduler is a background execution surface.  Keep the
        # application privacy configuration on the runner so webhook actions
        # cannot silently fall back to a process-default direct gateway.
        self._privacy_config = privacy_config
        self._execute_callback = execute_callback
        # Run history is observational.  Keep an injectable repository for
        # tests/alternate persistence backends and resolve the production
        # adapter lazily to preserve compatibility with pre-migration stores.
        self._history_repository = history_repository
        self._history_repository_checked = history_repository is not None

        self._check_interval = 60  # メインループのチェック間隔（秒）
        self._heartbeat_timeout_seconds = float(
            os.getenv("AOITALK_HEARTBEAT_TIMEOUT_SECONDS", "60")
        )

    def set_llm_client(self, llm_client) -> None:
        """既存server API互換用。Heartbeat実行経路では直接使用しない。"""
        self._llm_client = llm_client

    def set_execute_callback(
        self,
        callback: Optional[
            Callable[
                [HeartbeatDefinition, Dict[str, Any]],
                Awaitable[Mapping[str, Any]],
            ]
        ],
    ) -> None:
        """Heartbeatの唯一のagent/steward実行境界を設定する。"""
        self._execute_callback = callback

    def set_privacy_config(self, config: Any | None) -> None:
        """Set the app privacy configuration used by external actions."""

        self._privacy_config = config

    def set_broadcast_fn(self, fn: Callable[[Dict[str, Any]], Coroutine]) -> None:
        """WebSocketブロードキャスト関数を設定"""
        self._broadcast_fn = fn

    def set_admin_notify_fn(self, fn: Callable[[Dict[str, Any]], Coroutine]) -> None:
        """管理者向け通知ブロードキャスト関数を設定"""
        self._admin_notify_fn = fn

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is not None:
            value = value.astimezone().astimezone(tz=None).replace(tzinfo=None)
        return value

    async def _get_session(self):
        if self._session_factory is not None:
            session = self._session_factory()
            if asyncio.iscoroutine(session):
                session = await session
            return session

        from ..memory.database import get_database_manager

        return await get_database_manager().get_session()

    @staticmethod
    async def _close_session(session: Any) -> None:
        close = getattr(session, "close", None)
        if not callable(close):
            return
        result = close()
        if asyncio.iscoroutine(result):
            await result

    @staticmethod
    async def _rollback_quietly(session: Any) -> None:
        rollback = getattr(session, "rollback", None)
        if not callable(rollback):
            return
        try:
            result = rollback()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            pass

    def _resolve_history_repository(self) -> Any | None:
        """Resolve the optional durable run-history adapter.

        History is deliberately outside the scheduler state transaction.  A
        deployment can therefore run an older schema (or a unit-test SQLite
        database that only creates ``heartbeat_run_states``) while still
        making normal Heartbeat progress.  Callers treat a missing adapter or
        table as an observational failure and continue.
        """

        if self._history_repository_checked:
            return self._history_repository

        self._history_repository_checked = True
        repository_type = HeartbeatRunHistoryRepository
        if repository_type is None:
            try:
                from .history import HeartbeatRunHistoryRepository as repository_type
            except Exception:
                return None

        constructors = (
            lambda: repository_type(session_factory=self._session_factory),
            lambda: repository_type(self._session_factory),
            lambda: repository_type(),
        )
        for constructor in constructors:
            try:
                self._history_repository = constructor()
                return self._history_repository
            except TypeError:
                continue
            except Exception:
                logger.debug(
                    "Heartbeat history repository initialization failed",
                    exc_info=True,
                )
                return None
        return None

    @staticmethod
    def _history_identifier(value: Any) -> Any | None:
        """Extract a history row identifier from repository return values."""

        if value is None:
            return None
        if isinstance(value, Mapping):
            return value.get("id") or value.get("run_id") or value.get("history_id")
        return getattr(value, "id", None) or value

    async def _history_call(self, operation: str, **kwargs: Any) -> Any | None:
        """Invoke one repository operation without allowing errors to escape."""

        repository = self._resolve_history_repository()
        if repository is None:
            return None

        method = getattr(repository, operation, None)
        if not callable(method):
            return None

        # Repositories evolved while the history schema was being introduced.
        # Filter optional kwargs for older adapters, while retaining all values
        # for implementations that explicitly accept ``**kwargs``.
        call_kwargs = dict(kwargs)
        try:
            signature = inspect.signature(method)
            parameters = signature.parameters
            accepts_var_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
            if not accepts_var_kwargs:
                accepted = set(parameters)
                # The row identifier has appeared under all three names in
                # early adapters.  Translate it to the one the method uses.
                identifier = (
                    call_kwargs.get("history_id")
                    or call_kwargs.get("run_id")
                    or call_kwargs.get("id")
                )
                if identifier is not None:
                    for key in ("history_id", "run_id", "id"):
                        if key in accepted:
                            call_kwargs[key] = identifier
                call_kwargs = {
                    key: value
                    for key, value in call_kwargs.items()
                    if key in accepted
                }
        except (TypeError, ValueError):
            # Some C-extension/test doubles do not expose a signature.  They
            # generally accept **kwargs; let the invocation decide.
            pass

        try:
            result = method(**call_kwargs)
            if inspect.isawaitable(result):
                result = await result
            return result
        except Exception:
            # Logging must never change Heartbeat retry/cursor semantics.  A
            # missing table after a rolling deployment is expected, so keep
            # this at debug level rather than filling the application log.
            logger.debug(
                "Heartbeat run history operation failed: %s",
                operation,
                exc_info=True,
            )
            return None

    @staticmethod
    def _history_text(value: Any, *, limit: int) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        return text[:limit]

    @classmethod
    def _history_questions(cls, value: Any) -> list[dict[str, str]]:
        """Project only bounded, safe question metadata into run history."""

        if not isinstance(value, (list, tuple)):
            return []
        questions: list[dict[str, str]] = []
        for item in list(value)[:16]:
            if not isinstance(item, Mapping):
                continue
            title = cls._history_text(item.get("title"), limit=200)
            message = cls._history_text(item.get("message"), limit=500)
            urgency = cls._history_text(item.get("urgency"), limit=24) or "normal"
            if title is None and message is None:
                continue
            questions.append(
                {
                    "title": title or "Question",
                    "message": message or "",
                    "urgency": urgency,
                }
            )
        return questions

    @classmethod
    def _history_result_projection(
        cls,
        result: Mapping[str, Any] | None,
        *,
        status: str,
        next_cursor: Any = _CURSOR_UNSET,
        fallback_cursor: Any = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        # Keep one sanitizer as the source of truth for all history writes.
        # In particular, this prevents the direct-model compatibility path
        # from ever persisting raw question text, evidence-shaped keys, or
        # exception messages.
        source = dict(result) if isinstance(result, Mapping) else {}
        if _project_history_result is None:
            return {
                "status": "failed" if status not in {"ok", "success", "alert"} else "succeeded",
                "success": status in {"ok", "success", "alert"},
                "memory_upsert_count": 0,
                "forgotten_count": 0,
                "question_count": 0,
                "continuation_pending": bool(source.get("continuation_pending")),
                "forced": bool(source.get("forced")),
                "generic_action_count": 0,
                "generic_action_failure_count": 0,
                "result_summary": None,
                "questions_json": [],
                "safe_error_code": None,
                "error_code": None,
            }
        projected = _project_history_result(
            source,
            status=status,
            error_code=error_code,
        )
        return {
            **projected,
            # Compatibility aliases consumed by transitional repositories;
            # ``cursor_json`` and ``error_message`` are intentionally omitted.
            "questions_json": projected["questions"],
            "error_code": projected["safe_error_code"],
        }

    async def _history_start(
        self,
        heartbeat: HeartbeatDefinition,
        claim: Dict[str, Any],
    ) -> Any | None:
        """Best-effort append of a ``running`` history row after claiming."""

        values = {
            "heartbeat_name": heartbeat.name,
            "mode": heartbeat.mode,
            "scope_type": claim.get("scope_type"),
            "scope_id": claim.get("scope_id"),
            "project_id": claim.get("project_id"),
            "owner_user_id": claim.get("owner_user_id"),
            "started_at": claim.get("started_at"),
            "completed_at": None,
            "status": "running",
            "success": None,
            "forced": bool(claim.get("force")),
            "memory_upsert_count": 0,
            "forgotten_count": 0,
            "question_count": 0,
            "continuation_pending": False,
            "result_summary": None,
            "questions_json": [],
            "safe_error_code": None,
            "generic_action_count": 0,
            "generic_action_failure_count": 0,
        }
        started = await self._history_call("start_run", **values)
        if started is None:
            started = await self._history_call("record_start", **values)
        identifier = self._history_identifier(started)
        if identifier is not None:
            return identifier

        # Direct-model fallback keeps the adapter useful during a partial
        # deployment where the repository module is unavailable.  Filter by
        # reflected columns so small test doubles and transitional schemas are
        # tolerated without changing scheduler state.
        try:
            from ..memory.models import HeartbeatRunHistory
        except Exception:
            return None

        session = None
        try:
            session = await self._get_session()
            columns = set(HeartbeatRunHistory.__table__.columns.keys())
            row = HeartbeatRunHistory(
                **{
                    key: value
                    for key, value in values.items()
                    if key in columns
                }
            )
            session.add(row)
            await session.commit()
            return getattr(row, "id", None)
        except Exception:
            if session is not None:
                await self._rollback_quietly(session)
            return None
        finally:
            if session is not None:
                await self._close_session(session)

    async def _history_complete(
        self,
        heartbeat: HeartbeatDefinition,
        claim: Mapping[str, Any],
        *,
        result: Mapping[str, Any] | None,
        status: str,
        next_cursor: Any = _CURSOR_UNSET,
        error_code: str | None = None,
    ) -> None:
        """Best-effort terminalization; never raises into scheduler logic."""

        result_for_history = dict(result) if isinstance(result, Mapping) else {}
        result_for_history.setdefault("forced", bool(claim.get("force")))
        projection = self._history_result_projection(
            result_for_history,
            status=status,
            next_cursor=next_cursor,
            fallback_cursor=claim.get("cursor_json"),
            error_code=error_code,
        )
        completed_at = self._now()
        values = {
            "heartbeat_name": heartbeat.name,
            "mode": heartbeat.mode,
            "scope_type": claim.get("scope_type"),
            "scope_id": claim.get("scope_id"),
            "project_id": claim.get("project_id"),
            "owner_user_id": (
                result_for_history.get("owner_user_id")
                if isinstance(result_for_history, Mapping)
                else claim.get("owner_user_id")
            ),
            "started_at": claim.get("started_at"),
            "completed_at": completed_at,
            **projection,
        }
        # ``HeartbeatRunHistoryRepository.complete_run`` accepts a projected
        # result mapping.  Supplying this explicit mapping also keeps the
        # adapter compatible with repositories that expose only the compact
        # ``record_run`` contract instead of individual count parameters.
        values["result"] = {
            "status": projection["status"],
            "memory_upsert_count": projection["memory_upsert_count"],
            "forgotten_count": projection["forgotten_count"],
            "question_count": projection["question_count"],
            "continuation_pending": projection["continuation_pending"],
            "questions": projection["questions_json"],
            "result_summary": projection["result_summary"],
            "safe_error_code": projection["safe_error_code"],
        }
        history_id = claim.get("history_id")
        if history_id is not None:
            values["history_id"] = history_id
            values["run_id"] = history_id
            completed = await self._history_call("complete_run", **values)
            if completed is None:
                await self._history_call("record_complete", **values)
        else:
            recorded = await self._history_call("record_run", **values)
            if recorded is None:
                # Direct fallback for a repository-less partial deployment.
                try:
                    from ..memory.models import HeartbeatRunHistory
                    session = await self._get_session()
                    columns = set(HeartbeatRunHistory.__table__.columns.keys())
                    row = HeartbeatRunHistory(
                        **{
                            key: value
                            for key, value in values.items()
                            if key in columns and key not in {"history_id", "run_id"}
                        }
                    )
                    session.add(row)
                    await session.commit()
                except Exception:
                    if "session" in locals() and session is not None:
                        await self._rollback_quietly(session)
                finally:
                    if "session" in locals() and session is not None:
                        await self._close_session(session)

    async def _history_prune(self, heartbeat_name: str | None = None) -> None:
        """Apply repository retention policy without making it required."""

        # Let the repository's configured age/per-scope limits remain
        # authoritative; this call merely gives it an opportunity to clean up
        # after a terminal run.  Retention is observer work and is never part
        # of the scheduler state transaction.
        values: dict[str, Any] = {"heartbeat_name": heartbeat_name}
        purged = await self._history_call("purge", **values)
        if purged is None:
            await self._history_call("prune", **values)

    async def _reconcile_stale_history(self) -> int:
        """Mark orphaned ``running`` history rows failed at startup."""

        now = self._now()
        cutoff = now - HEARTBEAT_STALE_AFTER
        values = {
            "now": now,
            "cutoff": cutoff,
            "error_code": ERROR_STALE_RUNNING,
            "error_message": ERROR_STALE_RUNNING,
        }
        for operation in (
            "reconcile_stale_runs",
            "recover_stale_runs",
            "mark_stale_running",
        ):
            result = await self._history_call(operation, **values)
            if result is not None:
                try:
                    return int(result)
                except (TypeError, ValueError):
                    return 0

        # Repository-less fallback.  Missing table/schema errors are swallowed
        # just like the adapter path above.
        try:
            from sqlalchemy import select
            from ..memory.models import HeartbeatRunHistory
            session = await self._get_session()
            rows = (
                (
                    await session.execute(
                        select(HeartbeatRunHistory)
                        .where(
                            HeartbeatRunHistory.status == "running",
                            HeartbeatRunHistory.started_at < cutoff,
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                row.status = "stale"
                row.success = False
                row.completed_at = now
                row.safe_error_code = ERROR_STALE_RUNNING
                row.updated_at = now
            if rows:
                await session.commit()
            else:
                await session.rollback()
            return len(rows)
        except Exception:
            if "session" in locals() and session is not None:
                await self._rollback_quietly(session)
            return 0
        finally:
            if "session" in locals() and session is not None:
                await self._close_session(session)

    async def start(self) -> None:
        """バックグラウンドタスクを開始"""
        if self._running:
            return

        await self._recover_stale_running()
        try:
            await self._reconcile_stale_history()
        except Exception:
            # Defensive guard for custom repositories: history reconciliation
            # is observational and must never prevent scheduler startup.
            logger.debug(
                "Heartbeat run history startup reconciliation failed",
                exc_info=True,
            )
        await self._reschedule_project_steward_startup_catchup()
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("[HeartbeatRunner] 開始")

    async def stop(self) -> None:
        """バックグラウンドタスクを停止"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("[HeartbeatRunner] 停止")

    async def trigger(self, name: str) -> Optional[Dict[str, Any]]:
        """既存API互換の手動force実行。schedulerと同じstate transitionを使う。"""
        registry = get_heartbeat_registry()
        heartbeat = registry.get(name)
        if not heartbeat:
            return None

        scopes = self._unique_scopes(await self._execution_scopes(heartbeat))
        if not scopes:
            result = {
                "heartbeat_name": heartbeat.name,
                "executed_at": self._now().isoformat(),
                "status": "no_project_scope",
                "response": None,
                "is_alert": False,
                "action_results": [],
            }
            self._last_results[heartbeat.name] = result
            return result

        results = []
        for scope in scopes:
            result = await self._run_stateful(heartbeat, scope, force=True)
            if result is not None:
                results.append(result)

        return self._aggregate_scope_results(heartbeat, results)

    def get_status(self) -> Dict[str, Any]:
        """Runner全体のステータスを返す"""
        registry = get_heartbeat_registry()
        return {
            "running": self._running,
            "llm_client_set": self._llm_client is not None,
            "total_heartbeats": len(registry),
            "enabled_heartbeats": len(registry.get_enabled()),
            "last_results": self._last_results,
        }

    async def list_history(
        self,
        *,
        heartbeat_name: str | None = None,
        mode: str | None = None,
        project_id: uuid.UUID | str | None = None,
        statuses: List[str] | None = None,
        cursor: str | None = None,
        limit: int = 25,
    ) -> Any:
        """Read the bounded operational history through the runner adapter.

        This is intentionally a read-only convenience boundary for the API;
        the scheduler's mutable state and execution paths do not depend on it.
        A missing history table is surfaced to the caller so the API can return
        a safe 503 while normal Heartbeat scheduling continues.
        """

        repository = self._resolve_history_repository()
        if repository is None:
            raise RuntimeError("Heartbeat history is unavailable")
        method = getattr(repository, "list_runs", None) or getattr(
            repository, "list", None
        )
        if not callable(method):
            raise RuntimeError("Heartbeat history reader is unavailable")
        kwargs: dict[str, Any] = {
            "heartbeat_name": heartbeat_name,
            "mode": mode,
            "project_id": project_id,
            "statuses": statuses,
            "cursor": cursor,
            "limit": limit,
        }
        result = method(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def _run_loop(self) -> None:
        """メインループ: durable DB stateを定期確認する。"""
        logger.info("[HeartbeatRunner] バックグラウンドループ開始")
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[HeartbeatRunner] tickエラー: {e}")

            try:
                await asyncio.sleep(self._check_interval)
            except asyncio.CancelledError:
                break

    async def _tick(self) -> None:
        """1回のチェックサイクル。downtime分をinterval単位でreplayしない。"""
        registry = get_heartbeat_registry()
        enabled = registry.get_enabled()
        if not enabled:
            return

        for heartbeat in enabled:
            if not self._is_in_active_hours(heartbeat):
                continue

            scopes = self._unique_scopes(await self._execution_scopes(heartbeat))
            for scope in scopes:
                await self._run_stateful(heartbeat, scope, force=False)

    async def _execution_scopes(
        self,
        heartbeat: HeartbeatDefinition,
    ) -> List[Dict[str, Any]]:
        if heartbeat.mode != "project_steward":
            return [
                {
                    "scope_type": "global",
                    "scope_id": "global",
                    "project_id": None,
                }
            ]

        session = await self._get_session()
        try:
            rows = (
                (
                    await session.execute(
                        select(Project.id)
                        .where(
                            Project.deleted_at.is_(None),
                            Project.is_completed.is_(False),
                        )
                        .order_by(Project.id)
                    )
                )
                .scalars()
                .all()
            )
        finally:
            await self._close_session(session)

        return [
            {
                "scope_type": "project",
                "scope_id": str(project_id),
                "project_id": project_id,
            }
            for project_id in rows
        ]

    @staticmethod
    def _stable_jitter_seconds(*parts: Any, maximum: int) -> int:
        """Return deterministic jitter in the inclusive range ``[0, maximum]``."""
        if maximum < 0:
            raise ValueError("maximum must be non-negative")
        payload = "\0".join(str(part or "") for part in parts)
        digest = hashlib.sha256(payload.encode("utf-8")).digest()
        return int.from_bytes(digest, "big") % (maximum + 1)

    @classmethod
    def _initial_due_at(
        cls,
        heartbeat: HeartbeatDefinition,
        scope: Mapping[str, Any],
        now: datetime,
    ) -> datetime:
        """Spread new project steward scopes across their scheduling interval."""
        spread_seconds = max(60, int(heartbeat.interval_minutes) * 60)
        offset = 1 + cls._stable_jitter_seconds(
            heartbeat.name,
            scope["scope_type"],
            scope["scope_id"],
            "initial",
            maximum=spread_seconds - 1,
        )
        return now + timedelta(seconds=offset)

    @classmethod
    def _project_steward_startup_due_at(
        cls,
        heartbeat: HeartbeatDefinition,
        scope: Mapping[str, Any],
        now: datetime,
    ) -> datetime:
        """Spread startup catch-up for a Project Steward across its interval.

        A scope that was already due when the process stopped must not all run
        in the first scheduler tick after startup.  Use the same deterministic
        hashing strategy as initial state creation, but with a distinct salt so
        a scope's startup slot is stable without coupling it to its initial
        slot.  The one-second floor guarantees the returned timestamp is
        strictly in the future even when the scheduler clock has only
        second-level precision.
        """
        spread_seconds = max(60, int(heartbeat.interval_minutes) * 60)
        offset = 1 + cls._stable_jitter_seconds(
            heartbeat.name,
            scope["scope_type"],
            scope["scope_id"],
            "startup_catchup",
            maximum=spread_seconds - 1,
        )
        return now + timedelta(seconds=offset)

    @classmethod
    def _failure_retry_due_at(
        cls,
        *,
        heartbeat_name: str,
        scope_type: str,
        scope_id: str,
        now: datetime,
    ) -> datetime:
        """Schedule a failed scope for a deterministic 10–12 minute retry."""
        jitter = cls._stable_jitter_seconds(
            heartbeat_name,
            scope_type,
            scope_id,
            "retry",
            maximum=HEARTBEAT_RETRY_JITTER_SECONDS,
        )
        return now + HEARTBEAT_RETRY_DELAY + timedelta(seconds=jitter)

    @classmethod
    def _unique_scopes(
        cls,
        scopes: List[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Normalize scopes and remove duplicate identities while preserving order."""
        unique: List[Dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for scope in scopes:
            normalized = cls._normalize_scope(scope)
            identity = (normalized["scope_type"], normalized["scope_id"])
            if identity in seen:
                continue
            seen.add(identity)
            unique.append(normalized)
        return unique

    @staticmethod
    def _normalize_scope(scope: Mapping[str, Any]) -> Dict[str, Any]:
        scope_type = str(scope.get("scope_type") or "").strip().casefold()

        if scope_type == "global":
            return {
                "scope_type": "global",
                "scope_id": "global",
                "project_id": None,
            }

        if scope_type != "project":
            raise ValueError("unsupported Heartbeat scope_type")

        project_id = uuid.UUID(str(scope.get("project_id")))
        scope_id = str(project_id)
        supplied_scope_id = str(scope.get("scope_id") or "").strip()
        if supplied_scope_id and supplied_scope_id != scope_id:
            raise ValueError("Heartbeat project scope_id/project_id mismatch")

        return {
            "scope_type": "project",
            "scope_id": scope_id,
            "project_id": project_id,
        }

    async def _ensure_state(
        self,
        heartbeat: HeartbeatDefinition,
        scope: Mapping[str, Any],
    ) -> None:
        normalized = self._normalize_scope(scope)

        for attempt in range(2):
            session = await self._get_session()
            try:
                existing = await session.scalar(
                    select(HeartbeatRunState.id).where(
                        HeartbeatRunState.heartbeat_name == heartbeat.name,
                        HeartbeatRunState.scope_type
                        == normalized["scope_type"],
                        HeartbeatRunState.scope_id == normalized["scope_id"],
                    )
                )
                if existing is not None:
                    return

                now = self._now()
                next_due_at = (
                    self._initial_due_at(heartbeat, normalized, now)
                    if heartbeat.mode == "project_steward"
                    else None
                )
                session.add(
                    HeartbeatRunState(
                        heartbeat_name=heartbeat.name,
                        scope_type=normalized["scope_type"],
                        scope_id=normalized["scope_id"],
                        project_id=normalized["project_id"],
                        last_started_at=None,
                        last_completed_at=None,
                        last_success_at=None,
                        next_due_at=next_due_at,
                        status="idle",
                        error_message=None,
                        created_at=now,
                        updated_at=now,
                    )
                )

                try:
                    await session.commit()
                    return
                except IntegrityError:
                    await self._rollback_quietly(session)
                    if attempt == 0:
                        continue
                    raise
            finally:
                await self._close_session(session)

    async def _recover_stale_running(self) -> int:
        now = self._now()
        cutoff = now - HEARTBEAT_STALE_AFTER
        recovered = 0

        session = await self._get_session()
        try:
            rows = (
                (
                    await session.execute(
                        select(HeartbeatRunState)
                        .where(HeartbeatRunState.status == "running")
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )

            for state in rows:
                started_at = state.last_started_at
                if started_at is not None and started_at > cutoff:
                    continue

                state.status = "failed"
                state.error_message = ERROR_STALE_RUNNING
                state.last_completed_at = now
                state.next_due_at = self._failure_retry_due_at(
                    heartbeat_name=state.heartbeat_name,
                    scope_type=state.scope_type,
                    scope_id=state.scope_id,
                    now=now,
                )
                state.updated_at = now
                recovered += 1

            if recovered:
                await session.commit()
            else:
                await session.rollback()
        except Exception:
            await self._rollback_quietly(session)
            raise
        finally:
            await self._close_session(session)

        return recovered

    async def _reschedule_project_steward_startup_catchup(self) -> int:
        """Move overdue Project Steward scopes into future startup slots.

        Durable states can remain overdue while the service is down.  Running
        every such scope on the first scheduler tick causes a startup burst
        (and, when the provider is unavailable, an error storm).  Only known
        Project Steward definitions are touched; generic heartbeat semantics
        and legacy ``NULL`` due timestamps are left to the normal claim path.
        """
        project_stewards = {
            heartbeat.name: heartbeat
            for heartbeat in get_heartbeat_registry().get_all()
            if heartbeat.mode == "project_steward"
        }
        if not project_stewards:
            return 0

        now = self._now()
        session = await self._get_session()
        rescheduled = 0
        try:
            states = (
                (
                    await session.execute(
                        select(HeartbeatRunState)
                        .where(
                            HeartbeatRunState.scope_type == "project",
                            or_(
                                HeartbeatRunState.status == "idle",
                                HeartbeatRunState.status == "failed",
                            ),
                            HeartbeatRunState.next_due_at.is_not(None),
                            HeartbeatRunState.next_due_at <= now,
                            HeartbeatRunState.heartbeat_name.in_(
                                project_stewards.keys()
                            ),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )

            for state in states:
                heartbeat = project_stewards.get(state.heartbeat_name)
                if heartbeat is None:
                    continue

                scope = {
                    "scope_type": state.scope_type,
                    "scope_id": state.scope_id,
                    "project_id": state.project_id,
                }
                state.next_due_at = self._project_steward_startup_due_at(
                    heartbeat,
                    scope,
                    now,
                )
                state.updated_at = now
                rescheduled += 1

            if rescheduled:
                await session.commit()
            else:
                await session.rollback()
        except Exception:
            await self._rollback_quietly(session)
            raise
        finally:
            await self._close_session(session)

        return rescheduled

    async def _claim(
        self,
        heartbeat: HeartbeatDefinition,
        scope: Mapping[str, Any],
        *,
        force: bool,
    ) -> Optional[Dict[str, Any]]:
        normalized = self._normalize_scope(scope)
        await self._ensure_state(heartbeat, normalized)

        now = self._now()
        stale_cutoff = now - HEARTBEAT_STALE_AFTER

        session = await self._get_session()
        try:
            state = await session.scalar(
                select(HeartbeatRunState)
                .where(
                    HeartbeatRunState.heartbeat_name == heartbeat.name,
                    HeartbeatRunState.scope_type == normalized["scope_type"],
                    HeartbeatRunState.scope_id == normalized["scope_id"],
                )
                .with_for_update()
            )
            if state is None:
                await session.rollback()
                return None

            if state.status == "running":
                started_at = state.last_started_at
                if started_at is not None and started_at > stale_cutoff:
                    await session.rollback()
                    return None

                state.status = "failed"
                state.error_message = ERROR_STALE_RUNNING
                state.last_completed_at = now
                state.next_due_at = self._failure_retry_due_at(
                    heartbeat_name=state.heartbeat_name,
                    scope_type=state.scope_type,
                    scope_id=state.scope_id,
                    now=now,
                )
                state.updated_at = now

            if state.status not in {"idle", "failed"}:
                await session.rollback()
                return None

            if (
                not force
                and heartbeat.mode == "project_steward"
                and state.next_due_at is None
                and state.last_started_at is None
                and state.last_completed_at is None
            ):
                state.next_due_at = self._initial_due_at(heartbeat, normalized, now)
                state.updated_at = now
                await session.commit()
                return None

            if (
                not force
                and state.next_due_at is not None
                and state.next_due_at > now
            ):
                await session.rollback()
                return None

            previous_cursor = copy.deepcopy(state.cursor_json)
            previous_success_at = state.last_success_at

            state.status = "running"
            state.error_message = None
            state.last_started_at = now
            state.updated_at = now

            claim = {
                "state_id": state.id,
                "heartbeat_name": heartbeat.name,
                "scope_type": normalized["scope_type"],
                "scope_id": normalized["scope_id"],
                "project_id": normalized["project_id"],
                "started_at": now,
                "last_success_at": previous_success_at,
                "cursor_json": previous_cursor,
                "force": force,
            }

            await session.commit()
            return claim
        except Exception:
            await self._rollback_quietly(session)
            raise
        finally:
            await self._close_session(session)

    async def _complete_success(
        self,
        heartbeat: HeartbeatDefinition,
        claim: Mapping[str, Any],
        *,
        next_cursor: Any = _CURSOR_UNSET,
    ) -> None:
        completed_at = self._now()
        next_due_at = completed_at + timedelta(
            minutes=int(heartbeat.interval_minutes)
        )

        session = await self._get_session()
        try:
            state = await session.scalar(
                select(HeartbeatRunState)
                .where(HeartbeatRunState.id == claim["state_id"])
                .with_for_update()
            )
            if (
                state is None
                or state.status != "running"
                or state.last_started_at != claim["started_at"]
            ):
                await session.rollback()
                raise RuntimeError("heartbeat_claim_lost")

            state.status = "idle"
            state.error_message = None
            state.last_completed_at = completed_at
            state.last_success_at = completed_at
            state.next_due_at = next_due_at
            state.updated_at = completed_at

            if next_cursor is not _CURSOR_UNSET:
                state.cursor_json = next_cursor

            await session.commit()
        except Exception:
            await self._rollback_quietly(session)
            raise
        finally:
            await self._close_session(session)

    async def _complete_failure(
        self,
        claim: Mapping[str, Any],
        *,
        error_code: str,
    ) -> None:
        completed_at = self._now()

        session = await self._get_session()
        try:
            state = await session.scalar(
                select(HeartbeatRunState)
                .where(HeartbeatRunState.id == claim["state_id"])
                .with_for_update()
            )
            if (
                state is None
                or state.status != "running"
                or state.last_started_at != claim["started_at"]
            ):
                await session.rollback()
                return

            state.status = "failed"
            state.error_message = error_code
            state.last_completed_at = completed_at
            state.next_due_at = self._failure_retry_due_at(
                heartbeat_name=str(claim["heartbeat_name"]),
                scope_type=str(claim["scope_type"]),
                scope_id=str(claim["scope_id"]),
                now=completed_at,
            )
            state.updated_at = completed_at

            # failure/timeoutではlast_success_atとcursor_jsonを変更しない。
            await session.commit()
        except Exception:
            await self._rollback_quietly(session)
            raise
        finally:
            await self._close_session(session)

    async def _run_stateful(
        self,
        heartbeat: HeartbeatDefinition,
        scope: Mapping[str, Any],
        *,
        force: bool,
    ) -> Optional[Dict[str, Any]]:
        """Run one scope, serializing Project Steward executions globally."""
        if heartbeat.mode == "project_steward":
            async with _project_steward_execution_lock:
                return await self._run_stateful_locked(
                    heartbeat,
                    scope,
                    force=force,
                )

        return await self._run_stateful_locked(heartbeat, scope, force=force)

    async def _run_stateful_locked(
        self,
        heartbeat: HeartbeatDefinition,
        scope: Mapping[str, Any],
        *,
        force: bool,
    ) -> Optional[Dict[str, Any]]:
        if not force and not self._is_in_active_hours(heartbeat):
            return None

        claim = await self._claim(heartbeat, scope, force=force)
        if claim is None:
            return None

        executed_at = self._now()
        # Insert a running history row only after the scheduler claim has
        # committed.  A history outage must never turn a valid claim into a
        # retry or cursor mutation; terminalization is attempted below after
        # the authoritative state transition.
        history_id = await self._history_start(heartbeat, claim)
        if history_id is not None:
            claim["history_id"] = history_id

        if self._execute_callback is None:
            await self._complete_failure(
                claim,
                error_code=ERROR_EXECUTOR_UNAVAILABLE,
            )
            result = {
                "heartbeat_name": heartbeat.name,
                "executed_at": executed_at.isoformat(),
                "status": "executor_unavailable",
                "response": None,
                "is_alert": False,
                "action_results": [],
                "scope_type": claim["scope_type"],
                "scope_id": claim["scope_id"],
                "project_id": (
                    str(claim["project_id"])
                    if claim["project_id"] is not None
                    else None
                ),
            }
            await self._history_complete(
                heartbeat,
                claim,
                result=result,
                status=result["status"],
                error_code=ERROR_EXECUTOR_UNAVAILABLE,
            )
            await self._history_prune(heartbeat.name)
            self._last_results[heartbeat.name] = result
            return result

        execution_context = {
            "heartbeat_name": heartbeat.name,
            "mode": heartbeat.mode,
            "scope_type": claim["scope_type"],
            "scope_id": claim["scope_id"],
            "project_id": (
                str(claim["project_id"])
                if claim["project_id"] is not None
                else None
            ),
            "last_success_at": (
                claim["last_success_at"].isoformat()
                if claim["last_success_at"] is not None
                else None
            ),
            "cursor_json": copy.deepcopy(claim["cursor_json"]),
            "force": bool(force),
        }

        try:
            raw_result = await asyncio.wait_for(
                self._execute_callback(heartbeat, execution_context),
                timeout=self._heartbeat_timeout_seconds,
            )
        except asyncio.CancelledError:
            # shutdown時はrunningをそのまま残し、次回startupのstale recoveryへ委ねる。
            raise
        except asyncio.TimeoutError:
            await self._complete_failure(
                claim,
                error_code=ERROR_EXECUTION_TIMEOUT,
            )
            result = {
                "heartbeat_name": heartbeat.name,
                "executed_at": executed_at.isoformat(),
                "status": "timeout",
                "response": None,
                "is_alert": False,
                "action_results": [],
                "scope_type": claim["scope_type"],
                "scope_id": claim["scope_id"],
                "project_id": execution_context["project_id"],
            }
            await self._history_complete(
                heartbeat,
                claim,
                result=result,
                status=result["status"],
                error_code=ERROR_EXECUTION_TIMEOUT,
            )
            await self._history_prune(heartbeat.name)
            self._last_results[heartbeat.name] = result
            return result
        except Exception as exc:
            generation_failure = classify_generation_error(exc)
            if generation_failure.kind == GenerationErrorKind.INSUFFICIENT_QUOTA:
                logger.warning(
                    "[HeartbeatRunner] execution unavailable: heartbeat=%s scope=%s reason=%s",
                    heartbeat.name,
                    claim["scope_id"],
                    generation_failure.user_message,
                )
            else:
                logger.exception(
                    "[HeartbeatRunner] execution failed: heartbeat=%s scope=%s",
                    heartbeat.name,
                    claim["scope_id"],
                )
            await self._complete_failure(
                claim,
                error_code=ERROR_EXECUTION_FAILED,
            )
            result = {
                "heartbeat_name": heartbeat.name,
                "executed_at": executed_at.isoformat(),
                "status": "error",
                "response": None,
                "is_alert": False,
                "action_results": [],
                "scope_type": claim["scope_type"],
                "scope_id": claim["scope_id"],
                "project_id": execution_context["project_id"],
            }
            await self._history_complete(
                heartbeat,
                claim,
                result=result,
                status=result["status"],
                error_code=ERROR_EXECUTION_FAILED,
            )
            await self._history_prune(heartbeat.name)
            self._last_results[heartbeat.name] = result
            return result

        if not isinstance(raw_result, Mapping):
            await self._complete_failure(
                claim,
                error_code=ERROR_EXECUTION_FAILED,
            )
            result = {
                "heartbeat_name": heartbeat.name,
                "executed_at": executed_at.isoformat(),
                "status": "error",
                "response": None,
                "is_alert": False,
                "action_results": [],
                "scope_type": claim["scope_type"],
                "scope_id": claim["scope_id"],
                "project_id": execution_context["project_id"],
            }
            await self._history_complete(
                heartbeat,
                claim,
                result=result,
                status=result["status"],
                error_code=ERROR_EXECUTION_FAILED,
            )
            await self._history_prune(heartbeat.name)
            self._last_results[heartbeat.name] = result
            return result

        result = dict(raw_result)
        history_owner_id = result.pop("owner_user_id", None)
        next_cursor = result.pop("cursor_json", _CURSOR_UNSET)

        result.setdefault("heartbeat_name", heartbeat.name)
        result.setdefault("executed_at", executed_at.isoformat())
        result.setdefault("status", "ok")
        result.setdefault("response", HEARTBEAT_OK)
        result.setdefault("is_alert", result.get("status") == "alert")
        result["scope_type"] = claim["scope_type"]
        result["scope_id"] = claim["scope_id"]
        result["project_id"] = execution_context["project_id"]

        if (
            result.get("is_alert")
            and heartbeat.notify_channel == "websocket"
            and result.get("response")
        ):
            await self._notify(
                heartbeat,
                str(result["response"]),
                executed_at,
            )

        # custom actionsはscheduler stateから分離し、action failureをrun failureにしない。
        result["action_results"] = await self._run_actions(heartbeat, result)

        await self._complete_success(
            heartbeat,
            claim,
            next_cursor=next_cursor,
        )

        if history_owner_id is not None:
            # Keep owner information for ACL-aware history repositories without
            # exposing it through the existing ``last_result`` API payload.
            history_result = dict(result)
            history_result["owner_user_id"] = history_owner_id
        else:
            history_result = result
        await self._history_complete(
            heartbeat,
            claim,
            result=history_result,
            status=str(result.get("status") or "ok"),
            next_cursor=next_cursor,
        )
        await self._history_prune(heartbeat.name)

        self._last_results[heartbeat.name] = result
        return result

    def _aggregate_scope_results(
        self,
        heartbeat: HeartbeatDefinition,
        results: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if len(results) == 1:
            return results[0]

        statuses = [str(item.get("status") or "") for item in results]
        any_failure = any(
            status
            in {
                "error",
                "timeout",
                "executor_unavailable",
            }
            for status in statuses
        )
        any_alert = any(bool(item.get("is_alert")) for item in results)

        aggregate = {
            "heartbeat_name": heartbeat.name,
            "executed_at": self._now().isoformat(),
            "status": (
                "partial_failure"
                if any_failure
                else "alert"
                if any_alert
                else "ok"
            ),
            "response": None,
            "is_alert": any_alert,
            "action_results": [],
            "scope_results": results,
        }
        self._last_results[heartbeat.name] = aggregate
        return aggregate

    async def _run_actions(
        self, heartbeat: HeartbeatDefinition, result: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Heartbeatに紐づくアクションを実行する。

        action:
          type: run_script | run_skill | webhook | create_task | notify
          run_on: alert | ok | always (default: alert)
          config: アクション固有設定
        """
        action_results: List[Dict[str, Any]] = []
        for action in heartbeat.actions or []:
            if not isinstance(action, dict):
                continue
            action_type = str(action.get("type") or "").strip()
            run_on = str(action.get("run_on") or "alert").strip().lower()
            if not action_type:
                continue
            if run_on != "always" and run_on != result.get("status"):
                continue

            config = action.get("config") if isinstance(action.get("config"), dict) else {}
            started = datetime.utcnow().isoformat()
            try:
                output = await self._execute_action(action_type, config, result)
                action_results.append(
                    {
                        "type": action_type,
                        "status": "ok",
                        "started_at": started,
                        "result": output,
                    }
                )
            except Exception as exc:
                logger.error(
                    "[HeartbeatRunner] action failed: heartbeat=%s type=%s error=%s",
                    heartbeat.name,
                    action_type,
                    exc,
                )
                action_results.append(
                    {
                        "type": action_type,
                        "status": "error",
                        "started_at": started,
                        "error": str(exc),
                    }
                )
        return action_results

    async def _execute_action(
        self, action_type: str, config: Dict[str, Any], result: Dict[str, Any]
    ) -> Dict[str, Any]:
        if action_type == "run_script":
            return await self._action_run_script(config)
        if action_type == "run_skill":
            return await self._action_run_skill(config, result)
        if action_type == "webhook":
            return await self._action_webhook(config, result)
        if action_type == "create_task":
            return await self._action_create_task(config, result)
        if action_type == "notify":
            message = str(config.get("message") or result.get("response") or "")
            await self._notify(
                HeartbeatDefinition(
                    name=str(config.get("name") or "heartbeat-action"),
                    description="",
                    checklist="",
                ),
                message,
                datetime.utcnow(),
            )
            return {"status": "sent"}
        raise ValueError(f"Unsupported heartbeat action type: {action_type}")

    async def _action_run_script(self, config: Dict[str, Any]) -> Dict[str, Any]:
        command = config.get("command")
        if not command:
            raise ValueError("run_script requires command")
        timeout = int(config.get("timeout_seconds") or 300)
        cwd = str(config.get("cwd") or os.getcwd())
        env = os.environ.copy()
        extra_env = config.get("env")
        if isinstance(extra_env, dict):
            env.update({str(k): str(v) for k, v in extra_env.items()})

        if isinstance(command, list):
            proc = await asyncio.create_subprocess_exec(
                *[str(part) for part in command],
                cwd=cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                str(command),
                cwd=cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise TimeoutError(f"script timed out after {timeout}s")
        return {
            "returncode": proc.returncode,
            "stdout": stdout.decode("utf-8", errors="replace")[-4000:],
            "stderr": stderr.decode("utf-8", errors="replace")[-4000:],
        }

    async def _action_run_skill(
        self, config: Dict[str, Any], result: Dict[str, Any]
    ) -> Dict[str, Any]:
        skill_name = str(config.get("skill_name") or "")
        if not skill_name:
            raise ValueError("run_skill requires skill_name")
        input_text = str(config.get("input") or result.get("response") or "")
        from ..skills.executor import invoke_named_skill

        # Scheduler-owned Heartbeats normally have no ConversationMessage or
        # AgentRun and therefore intentionally produce no usage receipt. If a
        # run_skill action is executed inside an already trusted durable
        # TurnContext/AgentRun, the shared executor records that exact existing
        # provenance as ``heartbeat``. Never create a run/message here.
        rendered = await invoke_named_skill(
            skill_name=skill_name,
            input_text=input_text,
            invocation_path="heartbeat",
        )
        return {"skill_name": skill_name, "rendered": rendered}

    async def _action_webhook(
        self, config: Dict[str, Any], result: Dict[str, Any]
    ) -> Dict[str, Any]:
        from .safe_webhook import safe_webhook_request

        url = str(config.get("url") or "")
        if not url:
            raise ValueError("webhook requires url")
        method = str(config.get("method") or "POST").upper()
        payload = config.get("payload")
        if payload is None:
            payload = {
                "heartbeat": result.get("heartbeat_name"),
                "status": result.get("status"),
                "response": result.get("response"),
            }
        response = await safe_webhook_request(
            method,
            url,
            json_payload=payload,
            timeout_seconds=float(config.get("timeout_seconds") or 30),
            config=self._privacy_config,
        )
        response.raise_for_status()
        return {"status_code": response.status_code}

    async def _action_create_task(
        self, config: Dict[str, Any], result: Dict[str, Any]
    ) -> Dict[str, Any]:
        import uuid
        from ..memory.database import get_database_manager
        from ..services.task_management_service import TaskManagementService

        user_id = config.get("user_id")
        if not user_id:
            raise ValueError("create_task requires user_id")
        title = str(config.get("title") or f"Heartbeat alert: {result.get('heartbeat_name')}")
        description = str(config.get("description") or result.get("response") or "")
        project_id = config.get("project_id")

        db_manager = get_database_manager()
        session = await db_manager.get_session()
        try:
            service = TaskManagementService(broadcaster=self._broadcast_fn)
            task = await service.create_task(
                session,
                user_id=uuid.UUID(str(user_id)),
                title=title,
                description=description,
                project_id=uuid.UUID(str(project_id)) if project_id else None,
                status=str(config.get("status") or "todo"),
                priority=config.get("priority"),
                task_metadata={
                    "source": "heartbeat",
                    "heartbeat_name": result.get("heartbeat_name"),
                },
            )
            return {"task_id": str(task.get("id")), "title": task.get("title")}
        finally:
            await session.close()

    def _is_heartbeat_ok(self, response: str) -> bool:
        """レスポンスがHEARTBEAT_OKかどうか判定"""
        stripped = response.strip()
        return stripped.startswith(HEARTBEAT_OK) or stripped.endswith(HEARTBEAT_OK)

    def _is_in_active_hours(self, heartbeat: HeartbeatDefinition) -> bool:
        """active_hours内かどうか判定"""
        if not heartbeat.active_hours:
            return True

        start_str = heartbeat.active_hours.get("start")
        end_str = heartbeat.active_hours.get("end")
        if not start_str or not end_str:
            return True

        tz_name = heartbeat.active_hours.get("timezone")
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo(tz_name) if tz_name else None
        except Exception:
            tz = None

        now = datetime.now(tz)
        current_time = now.strftime("%H:%M")
        return start_str <= current_time < end_str

    async def _notify(self, heartbeat: HeartbeatDefinition, message: str, timestamp: datetime) -> None:
        """管理者接続のみへ WebSocket アラートを通知する。"""
        notify_fn = self._admin_notify_fn
        if not notify_fn:
            logger.error(
                "[HeartbeatRunner] admin notify callback is not configured; alert dropped"
            )
            return

        payload = {
            "type": "heartbeat_alert",
            "data": {
                "heartbeat_name": heartbeat.name,
                "message": message,
                "timestamp": timestamp.isoformat(),
            },
        }

        try:
            await notify_fn(payload)
        except Exception as e:
            logger.error(f"[HeartbeatRunner] 通知エラー: {e}")


# モジュールレベルのシングルトン
_global_runner = HeartbeatRunner()


def get_heartbeat_runner() -> HeartbeatRunner:
    return _global_runner
