"""Local Deep Research style iterative research service.

The implementation follows the Local Deep Research focused-iteration shape:
generate focused search questions, collect citation-ready sources, then
synthesize a Markdown report from the collected evidence. It is intentionally
adapted to AoiTalk's existing FastAPI/Next.js stack instead of vendoring the
full upstream application.
"""

from __future__ import annotations

import asyncio
import copy
import contextvars
import html
import json
import logging
import math
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
import xml.etree.ElementTree as ET

import httpx

from ..llm.agent_runtime import (
    OpenAIToolCallRecord,
    reset_verified_tool_execution_claims,
    set_verified_tool_execution_claims,
)
from ..llm.conversation_context import normalize_usage, persist_usage_sync
from ..llm.sglang_url import resolve_sglang_base_url, resolve_sglang_model
from .outbound_privacy_service import (
    EgressDescriptor,
    ExternalProviderBlocked,
    OutboundPrivacyGateway,
    PrivacyError,
    effective_privacy_mode,
    reset_privacy_policy_context,
    set_privacy_policy_context,
)
from .search_egress_policy import (
    SearchEgressPreconditionError,
    approved_public_egress,
    assert_public_search_egress_approved,
    is_enterprise_profile,
)
from .turn_context import get_turn_context, reset_turn_context, set_turn_context

from ..tools.external_llm_permission import (
    reset_permission_session_key,
    set_permission_session_key,
)

logger = logging.getLogger(__name__)


DEFAULT_ENGINES = ["searxng", "wikipedia", "arxiv", "openalex", "pubmed"]
SUPPORTED_ENGINES = frozenset(
    {
        "searxng",
        "duckduckgo",
        "yahoo_realtime",
        "wikipedia",
        "arxiv",
        "openalex",
        "pubmed",
        "local_knowledge",
    }
)
DEFAULT_YAHOO_REALTIME_URL = "https://search.yahoo.co.jp/realtime/search"
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}
MAX_RESEARCH_ENGINES = 16
_ENGINE_ALIASES = {
    "ddg": "duckduckgo",
    "duck_duck_go": "duckduckgo",
    "yahoo": "yahoo_realtime",
    "yahoo_realtime_search": "yahoo_realtime",
}

_PRIVACY_POLICY_KEYS = frozenset(
    {
        "privacy_mode",
        "review_policy",
        "semantic_redaction_enabled",
        "raw_media_policy",
        "trusted_local_hosts",
        "local_provider",
        "local_model",
    }
)
_PRIVACY_MODES = frozenset({"direct", "protected", "local_only"})


def _normalize_privacy_policy(value: Any, *, project_id: Optional[str] = None) -> dict[str, Any]:
    """Copy only the policy subset safe to persist into a research job."""

    if not isinstance(value, Mapping):
        result: dict[str, Any] = {}
    else:
        result = {}
        for key in _PRIVACY_POLICY_KEYS:
            if key not in value:
                continue
            candidate = value.get(key)
            if key == "privacy_mode":
                normalized = str(candidate or "").strip().lower()
                if normalized in _PRIVACY_MODES:
                    result[key] = normalized
            elif key == "semantic_redaction_enabled":
                if isinstance(candidate, bool):
                    result[key] = candidate
            elif key == "trusted_local_hosts":
                hosts = [candidate] if isinstance(candidate, str) else candidate
                if isinstance(hosts, (list, tuple, set, frozenset)):
                    result[key] = list(
                        dict.fromkeys(
                            str(item).strip().lower()
                            for item in hosts
                            if str(item).strip()
                        )
                    )[:32]
            elif isinstance(candidate, str):
                normalized = candidate.strip()
                if normalized and len(normalized) <= 128:
                    result[key] = normalized
    if project_id:
        result["project_id"] = str(project_id).strip()
    return result


def _privacy_snapshot(
    session_context: Any,
    project_metadata: Any,
    *,
    project_id: Optional[str] = None,
    global_policy: Any = None,
) -> dict[str, dict[str, Any]]:
    """Canonical immutable shape used for enqueue and worker drift checks."""

    global_snapshot: dict[str, Any] = {}
    if isinstance(global_policy, Mapping):
        global_snapshot = dict(global_policy)
        if "privacy_mode" not in global_snapshot and "mode" in global_snapshot:
            global_snapshot["privacy_mode"] = global_snapshot.get("mode")
    return {
        "session": _normalize_privacy_policy(session_context),
        "project": _normalize_privacy_policy(project_metadata, project_id=project_id),
        "global": _normalize_privacy_policy(global_snapshot),
    }

# Bounded worker/lifetime defaults.  Deployments can override these under the
# ``deep_research`` config namespace, but every stage remains finite even when
# no config object is available (for example in a direct CLI invocation).
DEFAULT_QUEUE_CAPACITY = 8
DEFAULT_WORKER_COUNT = 1
DEFAULT_PLANNING_TIMEOUT_SECONDS = 30.0
DEFAULT_ENGINE_TIMEOUT_SECONDS = 45.0
DEFAULT_SYNTHESIS_TIMEOUT_SECONDS = 90.0
DEFAULT_OVERALL_TIMEOUT_SECONDS = 180.0
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 5.0

DEEP_RESEARCH_ERROR_MESSAGES = {
    "scope_missing": "調査を開始できませんでした（会話セッションの指定が必要です）。",
    "scope_revoked": "調査を開始できませんでした（会話セッションへのアクセスが失効しました）。",
    "privacy_protection_failed": "調査を開始できませんでした（プライバシー保護に失敗しました）。",
    "queue_full": "調査を開始できませんでした（調査キューが混雑しています）。",
    "manager_shutdown": "調査サービスは現在停止中です。しばらくしてから再試行してください。",
    "planning_timeout": "調査計画が制限時間を超えました。",
    "engine_timeout": "検索エンジンが制限時間を超えました。",
    "synthesis_timeout": "レポート生成が制限時間を超えました。",
    "deadline": "調査が全体制限時間を超えました。",
    "process_restarted": "調査はサーバー再起動により中断されました。",
    "cancelled": "調査はキャンセルされました。",
    "internal_error": "調査に失敗しました。",
    "provider_failed": "調査プロバイダーが利用できませんでした。設定とサービス状態を確認してください。",
    "provider_invalid": "調査エンジンの設定が不正です。利用可能なエンジンを選択してください。",
    "egress_unreachable": "検索サービスに到達できませんでした。承認済みネットワーク経路を確認してください。",
    "credential_missing": "調査に必要な認証情報が設定されていません。",
}


def _consume_async_task(task: asyncio.Task[Any]) -> None:
    """Consume a detached coroutine result so late failures stay observed."""

    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:
        return


async def _await_bounded(awaitable: Awaitable[Any], timeout: float) -> Any:
    """Await an untrusted provider coroutine without cancellation hangs.

    ``asyncio.wait_for`` waits for cancellation propagation when the callee
    suppresses ``CancelledError``.  Provider/sidecar calls are instead run as
    a child task and detached after the finite deadline; their durable caller
    must terminalize the associated job before returning.
    """

    task = asyncio.ensure_future(awaitable)
    try:
        _done, pending = await asyncio.wait({task}, timeout=timeout)
        if pending:
            task.cancel()
            task.add_done_callback(_consume_async_task)
            raise asyncio.TimeoutError
        return task.result()
    except asyncio.CancelledError:
        if not task.done():
            task.cancel()
            task.add_done_callback(_consume_async_task)
        raise


class DeepResearchScopeError(RuntimeError):
    """A server-owned conversation scope is missing or no longer valid."""

    def __init__(self, code: str = "scope_missing") -> None:
        self.code = code if code in {"scope_missing", "scope_revoked"} else "scope_missing"
        super().__init__(self.code)


class DeepResearchStageTimeout(TimeoutError):
    """A planning/search/synthesis stage exceeded its finite deadline."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(stage)


class DeepResearchQueueFullError(RuntimeError):
    """Raised when the bounded research queue cannot accept another job."""

    code = "queue_full"


class DeepResearchTransportError(RuntimeError):
    """A search provider failed before yielding any usable transport result."""

    def __init__(self, code: str = "egress_unreachable") -> None:
        self.code = code if code in {"egress_unreachable", "engine_timeout"} else "egress_unreachable"
        super().__init__(self.code)


class DeepResearchCredentialError(RuntimeError):
    """Raised when an explicitly selected hosted model lacks credentials."""

    code = "credential_missing"


class DeepResearchProviderError(RuntimeError):
    """A model/provider returned no usable result or failed unexpectedly."""

    def __init__(self, code: str = "provider_failed") -> None:
        self.code = (
            code
            if code in {"provider_failed", "provider_invalid", "internal_error"}
            else "provider_failed"
        )
        super().__init__(self.code)


class DeepResearchManagerClosedError(RuntimeError):
    """Raised when a process-owned research manager is already shut down."""

    code = "manager_shutdown"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    # Mapping-backed Config objects commonly store nested dictionaries while
    # the runtime Config helper exposes dotted keys.  Prefer the nested form
    # before calling ``dict.get`` so ``{"deep_research": {"workers": 2}}``
    # is not mistaken for an absent value.
    if isinstance(config, Mapping):
        value: Any = config
        for part in key.split("."):
            if not isinstance(value, Mapping) or part not in value:
                break
            value = value[part]
        else:
            return value
    if hasattr(config, "get"):
        try:
            value = config.get(key, default)
            if value is not None:
                return value
        except Exception:
            pass
    return default


def _strip_html(text: str) -> str:
    clean = re.sub(r"<[^>]+>", " ", text or "")
    clean = html.unescape(clean)
    return re.sub(r"\s+", " ", clean).strip()


def _truncate(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _source_key(source: "DeepResearchSource") -> str:
    if source.url:
        return source.url.rstrip("/").lower()
    return f"{source.engine}:{source.title.lower()}"


def _normalize_duckduckgo_url(url: str) -> str:
    value = html.unescape(str(url or "")).strip()
    parsed = urlparse(value)
    if (
        (not parsed.netloc or "duckduckgo.com" in parsed.netloc)
        and parsed.path.startswith("/l/")
    ):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            return unquote(target)
    if value.startswith("//"):
        return f"https:{value}"
    return value


def _safe_filename(job_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", job_id)


@contextmanager
def _exclusive_job_file_lock(path: Path):
    """Serialize status read/replace transitions across local processes."""

    lock_path = path.with_name(f".{path.name}.lock")
    handle = lock_path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def _normalize_engine_id(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    return _ENGINE_ALIASES.get(normalized, normalized)


def _is_transport_failure(error: BaseException) -> bool:
    """Return whether an exception represents a failed provider transport.

    Parser/validation errors are deliberately excluded: an HTTP 200 response
    with no matching records is a valid empty result, whereas a connect,
    timeout, or HTTP status failure means the engine was not reachable.
    """

    return isinstance(error, DeepResearchTransportError) or isinstance(
        error,
        (
            asyncio.TimeoutError,
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.NetworkError,
            httpx.HTTPStatusError,
            OSError,
        ),
    )


def _provider_failure_code(error: BaseException, config: Any) -> str:
    """Map provider failures to stable, non-sensitive terminal codes."""

    if isinstance(error, DeepResearchTransportError):
        return error.code
    if _is_transport_failure(error):
        if isinstance(error, (asyncio.TimeoutError, httpx.TimeoutException)):
            return "engine_timeout"
        return "egress_unreachable"
    provider = str(_config_get(config, "llm_provider", "") or "").strip().lower()
    if provider in {"openai", "gemini"}:
        message = str(error).lower()
        if "api_key" in message or "credential" in message or "not configured" in message:
            return "credential_missing"
    return "provider_failed"


@dataclass
class DeepResearchEvent:
    timestamp: str
    message: str
    progress: int
    phase: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeepResearchSource:
    id: int
    title: str
    url: str
    snippet: str
    engine: str
    query: str
    published_at: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeepResearchRequest:
    query: str
    mode: str = "detailed"
    max_iterations: int = 3
    questions_per_iteration: int = 3
    max_results_per_query: int = 5
    engines: list[str] = field(default_factory=lambda: list(DEFAULT_ENGINES))
    include_local_knowledge: bool = False
    project_id: Optional[str] = None
    actor_user_id: Optional[str] = None
    is_admin: bool = False
    # Optional request scope used by direct/background callers.  HTTP routes
    # may omit these fields; the runner then falls back to the current
    # TurnContext and an isolated job id for privacy/usage scoping.
    session_id: Optional[str] = None
    session_context: Optional[Mapping[str, Any]] = None
    project_metadata: Optional[Mapping[str, Any]] = None

    def normalized(
        self,
        *,
        enterprise_public_egress_approved: Optional[bool] = None,
    ) -> "DeepResearchRequest":
        mode = self.mode if self.mode in {"quick", "detailed", "report"} else "detailed"
        default_iterations = {"quick": 1, "detailed": 3, "report": 4}[mode]
        max_iterations = self.max_iterations or default_iterations
        normalized_engines: list[str] = []
        for engine in self.engines or DEFAULT_ENGINES:
            normalized = _normalize_engine_id(engine)
            if not normalized or normalized in normalized_engines:
                continue
            normalized_engines.append(normalized)
        # Keep direct/non-HTTP callers bounded just like the API model.  A
        # sentinel preserves fail-closed validation instead of silently
        # dropping an attacker-supplied engine after the cap.
        if len(normalized_engines) > MAX_RESEARCH_ENGINES:
            normalized_engines = normalized_engines[:MAX_RESEARCH_ENGINES]
            normalized_engines.append("__too_many_engines__")
        # The historical default list contains public engines.  A fresh
        # Enterprise/local deployment must not silently turn that default into
        # public HTTP egress; SearXNG is the local authority and its own
        # configured endpoint/egress gate decides whether it can run.  An
        # explicitly approved public route can still be selected by enabling
        # public egress in the operator configuration.
        if (
            is_enterprise_profile()
            and enterprise_public_egress_approved is not True
            and normalized_engines == [
                _normalize_engine_id(e) for e in DEFAULT_ENGINES
            ]
        ):
            normalized_engines = ["searxng"]
        return DeepResearchRequest(
            query=self.query.strip(),
            mode=mode,
            max_iterations=max(1, min(int(max_iterations), 8)),
            questions_per_iteration=max(1, min(int(self.questions_per_iteration), 6)),
            max_results_per_query=max(1, min(int(self.max_results_per_query), 10)),
            engines=normalized_engines,
            include_local_knowledge=bool(self.include_local_knowledge),
            project_id=self.project_id,
            actor_user_id=self.actor_user_id,
            is_admin=bool(self.is_admin),
            session_id=str(self.session_id).strip() if self.session_id else None,
            session_context=_normalize_privacy_policy(self.session_context) or None,
            project_metadata=(
                _normalize_privacy_policy(
                    self.project_metadata,
                    project_id=self.project_id,
                )
                or None
            ),
        )


@dataclass
class DeepResearchJob:
    id: str
    user_id: str
    query: str
    status: str = "queued"
    progress: int = 0
    mode: str = "detailed"
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    # Durable actor/conversation scope.  ``metadata`` retains the historical
    # extensible projection, while these first-class fields make authorization
    # and client rendering independent of untrusted request payloads.
    actor_user_id: Optional[str] = None
    session_id: Optional[str] = None
    project_id: Optional[str] = None
    privacy_snapshot: dict[str, Any] = field(default_factory=dict)
    events: list[DeepResearchEvent] = field(default_factory=list)
    questions_by_iteration: dict[str, list[str]] = field(default_factory=dict)
    sources: list[DeepResearchSource] = field(default_factory=list)
    report_markdown: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def emit(
        self,
        message: str,
        progress: int,
        phase: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        self.progress = max(0, min(int(progress), 100))
        self.updated_at = _utc_now()
        self.events.append(
            DeepResearchEvent(
                timestamp=self.updated_at,
                message=message,
                progress=self.progress,
                phase=phase,
                metadata=metadata or {},
            )
        )

    def to_dict(self, include_report: bool = True) -> dict[str, Any]:
        payload = asdict(self)
        if not include_report:
            payload["report_markdown"] = ""
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeepResearchJob":
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        events = [
            DeepResearchEvent(**event)
            for event in data.get("events", [])
            if isinstance(event, dict)
        ]
        sources = [
            DeepResearchSource(**source)
            for source in data.get("sources", [])
            if isinstance(source, dict)
        ]
        return cls(
            id=data["id"],
            user_id=data.get("user_id", "unknown"),
            query=data.get("query", ""),
            status=data.get("status", "queued"),
            progress=int(data.get("progress", 0)),
            mode=data.get("mode", "detailed"),
            created_at=data.get("created_at", _utc_now()),
            updated_at=data.get("updated_at", _utc_now()),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            error=data.get("error"),
            error_code=data.get("error_code"),
            actor_user_id=data.get("actor_user_id")
            or metadata.get("actor_user_id"),
            session_id=data.get("session_id")
            or metadata.get("session_id"),
            project_id=data.get("project_id")
            or metadata.get("project_id"),
            privacy_snapshot=(
                dict(data.get("privacy_snapshot"))
                if isinstance(data.get("privacy_snapshot"), Mapping)
                else (
                    dict(metadata.get("privacy_snapshot"))
                    if isinstance(metadata.get("privacy_snapshot"), Mapping)
                    else {}
                )
            ),
            events=events,
            questions_by_iteration=data.get("questions_by_iteration", {}),
            sources=sources,
            report_markdown=data.get("report_markdown", ""),
            metadata=metadata,
        )


class DeepResearchJobStore:
    """Small JSON-backed store for restart-tolerant research history."""

    def __init__(self, base_dir: Path | str = "cache/deep_research") -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        # Jobs include private conversation queries, snippets and generated
        # reports.  Keep the cache process-private where the host filesystem
        # supports POSIX-style modes; Windows and restricted filesystems simply
        # retain their platform ACLs.
        try:
            self.base_dir.chmod(0o700)
        except OSError:
            pass

    def save(self, job: DeepResearchJob) -> None:
        path = self.base_dir / f"{_safe_filename(job.id)}.json"
        with _exclusive_job_file_lock(path):
            # A cancellation/restart tombstone is authoritative for this
            # local durable store. A provider coroutine detached during
            # shutdown may still hold an old in-memory job and attempt to
            # persist a late ``completed`` result; never let that stale writer
            # resurrect work. Same-status writes remain valid so a terminal
            # report can still be enriched with metadata by its owner.
            if path.exists():
                try:
                    existing = DeepResearchJob.from_dict(
                        json.loads(path.read_text(encoding="utf-8"))
                    )
                except Exception:
                    existing = None
                if (
                    existing is not None
                    and existing.status in TERMINAL_STATUSES
                    and job.status != existing.status
                ):
                    return
            payload = json.dumps(job.to_dict(), ensure_ascii=False, indent=2)
            # Write/replace atomically so a process crash cannot leave a
            # truncated JSON document that hides a terminal state or scope
            # metadata.
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{_safe_filename(job.id)}.",
                suffix=".tmp",
                dir=str(self.base_dir),
                text=True,
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    try:
                        os.fsync(handle.fileno())
                    except OSError:
                        pass
                os.replace(temp_name, path)
                try:
                    path.chmod(0o600)
                except OSError:
                    pass
            finally:
                try:
                    if os.path.exists(temp_name):
                        os.unlink(temp_name)
                except OSError:
                    pass

    def load(self, job_id: str) -> Optional[DeepResearchJob]:
        path = self.base_dir / f"{_safe_filename(job_id)}.json"
        if not path.exists():
            return None
        try:
            return DeepResearchJob.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:
            logger.warning("Deep research job load failed: %s", exc)
            return None

    def list_jobs(self, limit: int = 30, user_id: Optional[str] = None) -> list[DeepResearchJob]:
        jobs: list[DeepResearchJob] = []
        for path in self.base_dir.glob("*.json"):
            try:
                job = DeepResearchJob.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
            if user_id and job.user_id != user_id:
                continue
            jobs.append(job)
        jobs.sort(key=lambda item: item.created_at, reverse=True)
        return jobs[:limit]

    def reconcile_stale_jobs(self) -> int:
        """Terminalize jobs left queued/running by a previous process.

        The JSON store has no distributed lease, so a restarted process cannot
        safely resume a job that may still be running elsewhere.  Reconcile
        those records once at manager startup; never auto-retry an external
        search or model call from an untrusted checkpoint.
        """

        reconciled = 0
        for path in self.base_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                job = DeepResearchJob.from_dict(data)
            except Exception:
                continue
            if job.status not in {"queued", "running"}:
                continue
            job.status = "failed"
            job.error_code = "process_restarted"
            job.error = DEEP_RESEARCH_ERROR_MESSAGES["process_restarted"]
            job.completed_at = _utc_now()
            job.emit(
                "調査はサーバー再起動により中断されました",
                job.progress,
                "failed",
                {"error_code": "process_restarted"},
            )
            self.save(job)
            reconciled += 1
        return reconciled


class DeepResearchLLMAdapter:
    """Tool-free LLM adapter for research planning and synthesis."""

    def __init__(
        self,
        config: Any,
        user_id: str = "default_user",
        *,
        session_id: Optional[str] = None,
        project_id: Optional[str] = None,
        session_context: Optional[Mapping[str, Any]] = None,
        project_metadata: Optional[Mapping[str, Any]] = None,
        agent_name: str = "deep_research",
        request_type: str = "deep_research",
    ) -> None:
        self.config = config
        self.user_id = user_id
        self.session_id = session_id
        self.project_id = project_id
        self.session_context = (
            dict(session_context) if isinstance(session_context, Mapping) else None
        )
        self.project_metadata = (
            dict(project_metadata) if isinstance(project_metadata, Mapping) else None
        )
        if self.project_id:
            self.project_metadata = dict(self.project_metadata or {})
            self.project_metadata.setdefault("project_id", self.project_id)
        self.agent_name = agent_name
        self.request_type = request_type
        self._recorded_usage_responses: list[Any] = []
        self._deployment = None
        self._apply_deployment_contract()
        self._privacy_gateway = OutboundPrivacyGateway(
            self.config,
            user_id=str(user_id or "default_user"),
            session_id=str(session_id or ""),
            session_context=self.session_context,
            project_metadata=self.project_metadata,
        )

    @staticmethod
    def _is_enterprise() -> bool:
        try:
            from ..features import Features

            return bool(Features.is_enterprise())
        except Exception:
            return any(
                str(os.getenv(name) or "").strip().lower() == "enterprise"
                for name in ("AOITALK_PROFILE", "AIVTUBER_ENV")
            )

    @staticmethod
    def _model_descriptor(
        *,
        action: str,
        transport: str,
        destination: str,
        provider: str,
        model: str = "",
    ) -> EgressDescriptor:
        """Describe one direct research-model transaction."""

        return EgressDescriptor(
            action=str(action or "deep_research_model_request"),
            transport=str(transport or "provider"),
            destination=str(destination or provider),
            provider=str(provider or ""),
            tool="deep_research",
            model=str(model or ""),
        )

    async def _execute_model_request(
        self,
        payload: Mapping[str, Any],
        *,
        provider: str,
        base_url: str,
        source_kind: str,
        descriptor: EgressDescriptor,
        sender: Callable[[Any], Any],
        model: str = "",
    ) -> Any:
        """Route one direct model call through the transaction gateway.

        Keeping the sender nested makes the gateway's review result the sole
        authority for the wire payload.  There is deliberately no
        ``protect``-then-send fallback when an injected legacy gateway lacks
        ``execute``.
        """

        execute = getattr(self._privacy_gateway, "execute", None)
        if not callable(execute):
            raise PrivacyError("outbound privacy gateway does not support execution")
        return await execute(
            dict(payload),
            provider=provider,
            descriptor=descriptor,
            sender=sender,
            base_url=base_url,
            source_kind=source_kind,
            model=model,
        )

    def _apply_deployment_contract(self) -> None:
        """Project fixed Enterprise settings onto direct research SDK paths."""

        from ..llm.deployment_resolver import (
            effective_config_overrides,
            resolve_llm_deployment,
        )

        deployment = resolve_llm_deployment(self.config)
        self._deployment = deployment
        if deployment is None:
            return

        persisted_provider = str(
            _config_get(self.config, "llm_provider", "gemini") or "gemini"
        ).strip().lower()
        available, _ = deployment.provider_available(persisted_provider)
        if deployment.fixed or not available:
            # Deep Research does not accept a per-request provider override;
            # an out-of-contract persisted provider is therefore stale state,
            # not an explicit engine switch.  Use the effective endpoint/model
            # and leave the persisted config untouched for diagnostics.
            overrides = effective_config_overrides(self.config)
            if overrides:
                from ..llm.manager import TargetConfig

                self.config = TargetConfig(self.config, overrides)

    def set_usage_context(
        self,
        *,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        project_id: Optional[str] = None,
        session_context: Optional[Mapping[str, Any]] = None,
        project_metadata: Optional[Mapping[str, Any]] = None,
        agent_name: Optional[str] = None,
        request_type: Optional[str] = None,
    ) -> None:
        """Attach the research job scope to direct SDK usage rows.

        The normal provider client owns usage persistence.  Direct SDK calls
        in this adapter do not have that client, so keep the job scope here
        and pass it through a lightweight persistence proxy below.
        """

        if user_id:
            self.user_id = str(user_id)
        if session_id is not None:
            self.session_id = str(session_id) if session_id else None
        if project_id is not None:
            self.project_id = str(project_id) if project_id else None
        if session_context is not None:
            self.session_context = dict(session_context)
        if project_metadata is not None:
            self.project_metadata = dict(project_metadata)
        if self.project_id:
            self.project_metadata = dict(self.project_metadata or {})
            self.project_metadata.setdefault("project_id", self.project_id)
        if agent_name:
            self.agent_name = str(agent_name)
        if request_type:
            self.request_type = str(request_type)
        # The adapter may be created before the runner knows the durable
        # conversation scope. Keep the privacy gateway bound to the same
        # actor/session/project context as usage telemetry rather than leaving
        # a process-wide anonymous alias bucket behind.
        old_identity = (
            self._privacy_gateway.user_id,
            self._privacy_gateway.session_id,
        )
        new_identity = (str(self.user_id or "default_user"), str(self.session_id or ""))
        if old_identity != new_identity:
            self._privacy_gateway._raw_to_alias.clear()
            self._privacy_gateway._alias_to_raw.clear()
            self._privacy_gateway._counters.clear()
        self._privacy_gateway.user_id, self._privacy_gateway.session_id = new_identity
        self._privacy_gateway.update_policy_context(
            session_context=self.session_context,
            project_metadata=self.project_metadata,
        )

    class _UsageClient:
        def __init__(
            self,
            *,
            user_id: Optional[str],
            session_id: Optional[str],
            project_id: Optional[str],
            agent_name: Optional[str],
        ) -> None:
            self.current_session_id = session_id
            self.current_project_id = project_id
            self.character_name = agent_name
            self._user_id = user_id

        def _get_session_user_id(self) -> str:
            return str(self._user_id or "default_user")

    @staticmethod
    def _as_mapping(value: Any) -> dict[str, Any]:
        if isinstance(value, Mapping):
            return {str(key): item for key, item in value.items()}
        for method_name in ("model_dump", "to_dict", "dict"):
            method = getattr(value, method_name, None)
            if callable(method):
                try:
                    dumped = method()
                except Exception:
                    continue
                if isinstance(dumped, Mapping):
                    return {str(key): item for key, item in dumped.items()}
        raw = getattr(value, "__dict__", None)
        if isinstance(raw, Mapping):
            return {
                str(key): item
                for key, item in raw.items()
                if not str(key).startswith("_")
            }
        return {}

    @classmethod
    def _response_usage(cls, response: Any, *, provider: str) -> dict[str, Any]:
        """Normalize a successful direct SDK response without inventing usage."""

        raw = getattr(response, "usage", None)
        if raw is None:
            raw = getattr(response, "usage_metadata", None)
        if raw is None:
            raw = getattr(response, "usageMetadata", None)
        if raw is None and isinstance(response, Mapping):
            raw = (
                response.get("usage")
                or response.get("usage_metadata")
                or response.get("usageMetadata")
            )
        if raw is None:
            return {}

        payload = cls._as_mapping(raw)
        # google-generativeai exposes usage_metadata with Gemini-specific names.
        if not payload:
            for source, target in (
                ("prompt_token_count", "input_tokens"),
                ("promptTokenCount", "input_tokens"),
                ("candidates_token_count", "output_tokens"),
                ("candidatesTokenCount", "output_tokens"),
                ("cached_content_token_count", "cache_read_tokens"),
                ("cachedContentTokenCount", "cache_read_tokens"),
                ("cached_content_token_count", "cached_tokens"),
                ("cachedContentTokenCount", "cached_tokens"),
                ("thoughts_token_count", "reasoning_tokens"),
                ("thoughtsTokenCount", "reasoning_tokens"),
            ):
                value = getattr(raw, source, None)
                if value is not None:
                    payload[target] = value
        else:
            # model_dump() can preserve Gemini names; map them before the
            # common normalizer so zero/None semantics stay provider-owned.
            aliases = {
                "prompt_token_count": "input_tokens",
                "promptTokenCount": "input_tokens",
                "candidates_token_count": "output_tokens",
                "candidatesTokenCount": "output_tokens",
                "cached_content_token_count": "cache_read_tokens",
                "cachedContentTokenCount": "cache_read_tokens",
                "thoughts_token_count": "reasoning_tokens",
                "thoughtsTokenCount": "reasoning_tokens",
            }
            for source, target in aliases.items():
                if source in payload and target not in payload:
                    payload[target] = payload[source]
            for cache_key in ("cached_content_token_count", "cachedContentTokenCount"):
                if cache_key in payload and "cached_tokens" not in payload:
                    payload["cached_tokens"] = payload[cache_key]

        resolved_model = getattr(response, "model", None) or getattr(
            response, "model_version", None
        )
        if resolved_model is None and isinstance(response, Mapping):
            resolved_model = (
                response.get("model")
                or response.get("model_version")
                or response.get("modelVersion")
            )
        normalized = normalize_usage(
            payload,
            provider=provider,
            resolved_model=str(resolved_model) if resolved_model else None,
        )
        # ``normalize_usage`` intentionally leaves unavailable fields as None.
        # Do not persist a row when the provider gave no token dimensions.
        if normalized.get("input_tokens") is None and normalized.get("output_tokens") is None:
            return {}
        return normalized

    def _record_direct_usage(
        self,
        response: Any,
        *,
        provider: str,
        model: str,
        started: float,
    ) -> None:
        try:
            usage = self._response_usage(response, provider=provider)
            if not usage:
                return
            if self._mark_usage_recorded(response):
                return
            turn = get_turn_context()
            user_id = self.user_id or turn.user_id
            session_id = self.session_id or turn.session_id
            project_id = self.project_id or turn.project_id
            proxy = self._UsageClient(
                user_id=user_id,
                session_id=session_id,
                project_id=project_id,
                agent_name=self.agent_name,
            )
            persist_usage_sync(
                proxy,
                provider=provider,
                model=model,
                requested_model=model,
                resolved_model=usage.get("resolved_model"),
                usage=usage,
                request_type=self.request_type,
                latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
            )
        except Exception:
            # Usage telemetry must never turn a successful research response
            # into a failed job.
            logger.debug("Direct deep research usage persistence failed", exc_info=True)

    def _apply_client_usage_context(self, client: Any) -> None:
        """Apply research scope to a newly-created fallback provider client."""

        if client is None:
            return
        turn = get_turn_context()
        user_id = self.user_id or turn.user_id
        session_id = self.session_id or turn.session_id
        project_id = self.project_id or turn.project_id
        try:
            setter = getattr(client, "set_session_context", None)
            if callable(setter) and user_id:
                setter(user_id=str(user_id))
        except Exception:
            logger.debug("Deep research fallback user context setup failed", exc_info=True)
        if self.agent_name and hasattr(client, "character_name"):
            try:
                client.character_name = self.agent_name
            except Exception:
                logger.debug("Deep research fallback agent context setup failed", exc_info=True)
        for attribute, value in (
            ("current_session_id", session_id),
            ("current_project_id", project_id),
        ):
            if value is None or not hasattr(client, attribute):
                continue
            try:
                setattr(client, attribute, str(value))
            except Exception:
                logger.debug(
                    "Deep research fallback %s context setup failed",
                    attribute,
                    exc_info=True,
                )

    def _mark_usage_recorded(self, response: Any) -> bool:
        """Avoid duplicate rows when a caller reuses one SDK response."""

        try:
            if getattr(response, "_aoitalk_usage_recorded", False):
                return True
            setattr(response, "_aoitalk_usage_recorded", True)
            return False
        except Exception:
            recorded = getattr(self, "_recorded_usage_responses", None)
            if recorded is None:
                recorded = []
                self._recorded_usage_responses = recorded
            if any(item is response for item in recorded):
                return True
            recorded.append(response)
            del recorded[:-8]
            return False

    async def generate(self, prompt: str, *, max_tokens: int = 2048) -> str:
        provider = str(_config_get(self.config, "llm_provider", "gemini")).strip().lower()
        try:
            if provider == "gemini":
                return await self._generate_gemini(prompt)
            if provider == "openai":
                return await self._generate_openai(prompt, max_tokens=max_tokens)
            if provider == "openai_compatible_local" and self._deployment is not None:
                return await self._generate_openai_compatible(
                    prompt,
                    base_url=(
                        _config_get(self.config, "runtime.target_base_url", "")
                        or _config_get(
                            self.config,
                            "openai_compatible_local.base_url",
                            "http://127.0.0.1:8080/v1",
                        )
                    ),
                    api_key=(
                        _config_get(self.config, "runtime.target_api_key", "")
                        or _config_get(
                            self.config,
                            "openai_compatible_local.api_key",
                            "dummy",
                        )
                    ),
                    model=(
                        _config_get(self.config, "runtime.target_model", "")
                        or _config_get(
                            self.config,
                            "openai_compatible_local.model",
                            "local-model",
                        )
                    ),
                    max_tokens=max_tokens,
                )
            if provider == "ollama":
                return await self._generate_openai_compatible(
                    prompt,
                    base_url=_config_get(self.config, "ollama.base_url", "http://127.0.0.1:11434/v1"),
                    api_key=_config_get(self.config, "ollama.api_key", "ollama"),
                    model=_config_get(self.config, "ollama.model", None)
                    or _config_get(self.config, "llm_model", "gemma4:e4b"),
                    max_tokens=max_tokens,
                )
            if provider == "sglang":
                return await self._generate_openai_compatible(
                    prompt,
                    base_url=resolve_sglang_base_url(self.config),
                    api_key="sglang",
                    model=resolve_sglang_model(self.config, fallback="default"),
                    max_tokens=max_tokens,
                )
        except PrivacyError:
            # A blocked/redaction-failed outbound request must never fall back
            # to the existing client, which could bypass this adapter's
            # transport gate and send the raw research prompt externally.
            raise
        except Exception as exc:
            logger.warning("Direct deep research LLM call failed: %s", exc)
            # Enterprise deployments have one operator-selected provider.  A
            # failed call must not silently fall through to a different,
            # potentially unapproved client/configuration.
            if self._is_enterprise():
                raise

        return await self._generate_with_existing_client(prompt)

    async def _generate_gemini(self, prompt: str) -> str:
        api_key = (
            _config_get(self.config, "gemini_api_key", "")
            or os.getenv("GEMINI_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
        )
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")
        import google.generativeai as genai

        genai.configure(api_key=api_key)
        model_name = _config_get(self.config, "llm_model", "gemini-3-flash-preview")
        model = genai.GenerativeModel(model_name=model_name)
        started = time.perf_counter()
        descriptor = self._model_descriptor(
            action="deep_research_model_request",
            transport="google.generativeai.GenerativeModel.generate_content",
            destination="gemini",
            provider="gemini",
            model=str(model_name),
        )

        async def send(final_payload: Any) -> Any:
            if not isinstance(final_payload, Mapping):
                raise PrivacyError("deep research Gemini payload is malformed")
            outbound_prompt = final_payload.get("prompt")
            if not isinstance(outbound_prompt, str) or not outbound_prompt:
                raise PrivacyError("deep research Gemini payload has no prompt")
            if hasattr(model, "generate_content_async"):
                return await model.generate_content_async(outbound_prompt)
            return await asyncio.to_thread(model.generate_content, outbound_prompt)

        response = await self._execute_model_request(
            {"prompt": prompt},
            provider="gemini",
            base_url="",
            source_kind="deep_research_model_request",
            descriptor=descriptor,
            sender=send,
            model=str(model_name),
        )
        self._record_direct_usage(
            response,
            provider="gemini",
            model=str(model_name),
            started=started,
        )
        return self._privacy_gateway.restore(getattr(response, "text", str(response)) or "")

    async def _generate_openai(self, prompt: str, *, max_tokens: int) -> str:
        api_key = _config_get(self.config, "openai_api_key", "") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=api_key)
        model_name = str(_config_get(self.config, "llm_model", "gpt-4o"))
        started = time.perf_counter()
        request_kwargs = {
            "model": model_name,
            "instructions": "You write concise, citation-grounded research reports.",
            "input": prompt,
            "temperature": 0.2,
            "max_output_tokens": max_tokens,
        }
        base_url = str(getattr(client, "base_url", "") or "")
        descriptor = self._model_descriptor(
            action="deep_research_model_request",
            transport="openai.AsyncOpenAI.responses.create",
            destination=base_url or "https://api.openai.com/v1",
            provider="openai",
            model=model_name,
        )
        try:
            async def send(final_payload: Any) -> Any:
                if not isinstance(final_payload, Mapping):
                    raise PrivacyError("deep research OpenAI payload is malformed")
                return await client.responses.create(**dict(final_payload))

            response = await self._execute_model_request(
                request_kwargs,
                provider="openai",
                base_url=base_url,
                source_kind="deep_research_model_request",
                descriptor=descriptor,
                sender=send,
                model=model_name,
            )
            self._record_direct_usage(
                response,
                provider="openai",
                model=model_name,
                started=started,
            )
            return self._privacy_gateway.restore(getattr(response, "output_text", "") or "")
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    await close()
                except Exception:
                    logger.debug("Deep research OpenAI client cleanup failed", exc_info=True)

    async def _generate_openai_compatible(
        self,
        prompt: str,
        *,
        base_url: str,
        api_key: str,
        model: str,
        max_tokens: int,
    ) -> str:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(base_url=base_url.rstrip("/"), api_key=api_key or "local")
        started = time.perf_counter()
        request_kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": "You write concise, citation-grounded research reports."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        provider = str(
            _config_get(self.config, "llm_provider", "openai_compatible_local")
        )
        descriptor = self._model_descriptor(
            action="deep_research_model_request",
            transport="openai.AsyncOpenAI.chat.completions.create",
            destination=base_url,
            provider=provider,
            model=str(model),
        )
        try:
            async def send(final_payload: Any) -> Any:
                if not isinstance(final_payload, Mapping):
                    raise PrivacyError("deep research compatible-model payload is malformed")
                return await client.chat.completions.create(**dict(final_payload))

            response = await self._execute_model_request(
                request_kwargs,
                provider=provider,
                base_url=base_url,
                source_kind="deep_research_model_request",
                descriptor=descriptor,
                sender=send,
                model=str(model),
            )
            self._record_direct_usage(
                response,
                provider=provider,
                model=str(model),
                started=started,
            )
            return self._privacy_gateway.restore(response.choices[0].message.content or "")
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    await close()
                except Exception:
                    logger.debug("Deep research local client cleanup failed", exc_info=True)

    async def _generate_with_existing_client(self, prompt: str) -> str:
        provider = str(_config_get(self.config, "llm_provider", "gemini")).strip().lower()
        base_url = ""
        if provider == "openai":
            base_url = str(_config_get(self.config, "openai_base_url", "") or "")
        elif provider in {"ollama", "sglang", "openai_compatible_local"}:
            base_url = str(
                _config_get(self.config, f"{provider}.base_url", "")
                or _config_get(self.config, f"{provider}_base_url", "")
                or ""
            )
        # Existing-client fallback is still an outbound model transport.  Do
        # the same local-only preflight here so a direct-provider failure cannot
        # silently route the raw prompt through an un-gated client.
        self._privacy_gateway.ensure_provider_allowed(provider, base_url=base_url)
        from ..llm.manager import create_llm_client

        client = create_llm_client(self.config)
        self._apply_client_usage_context(client)
        if hasattr(client, "clear_history"):
            client.clear_history()
        descriptor = self._model_descriptor(
            action="deep_research_model_request_fallback",
            transport="llm.client.generate_response",
            destination=base_url or provider,
            provider=provider,
            model=str(_config_get(self.config, "llm_model", "") or ""),
        )

        async def send(final_payload: Any) -> Any:
            if not isinstance(final_payload, Mapping):
                raise PrivacyError("deep research fallback payload is malformed")
            outbound_prompt = final_payload.get("prompt")
            if not isinstance(outbound_prompt, str) or not outbound_prompt:
                raise PrivacyError("deep research fallback payload has no prompt")
            if hasattr(client, "generate_response_async"):
                return await client.generate_response_async(outbound_prompt)
            return await asyncio.to_thread(client.generate_response, outbound_prompt)

        result = await self._execute_model_request(
            {"prompt": prompt},
            provider=provider,
            base_url=base_url,
            source_kind="deep_research_model_request_fallback",
            descriptor=descriptor,
            sender=send,
            model=str(_config_get(self.config, "llm_model", "") or ""),
        )
        return self._privacy_gateway.restore(result or "")


class DeepResearchSearchClient:
    """Citation-ready source collection across local/free search engines."""

    def __init__(
        self,
        config: Any = None,
        timeout_seconds: float = 15.0,
        *,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        project_id: Optional[str] = None,
        session_context: Optional[Mapping[str, Any]] = None,
        project_metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.config = config
        self.timeout = httpx.Timeout(timeout_seconds, connect=8.0)
        self.user_id = str(user_id or "")
        self.session_id = str(session_id or "")
        self.project_id = str(project_id or "") or None
        self.session_context = (
            dict(session_context) if isinstance(session_context, Mapping) else None
        )
        self.project_metadata = (
            dict(project_metadata) if isinstance(project_metadata, Mapping) else None
        )
        # Reachability is an observation, not configuration.  Listing engines
        # never performs a network probe; entries stay ``unknown`` until an
        # actual search transport succeeds or fails.
        self._engine_observations: dict[str, dict[str, Any]] = {}
        self._privacy_gateway = OutboundPrivacyGateway(
            config,
            user_id=self.user_id,
            session_id=self.session_id,
            session_context=self.session_context,
            project_metadata=self.project_metadata,
        )

    def bind_context(
        self,
        *,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        project_id: Optional[str] = None,
        session_context: Optional[Mapping[str, Any]] = None,
        project_metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Bind one research job to the outbound search privacy scope.

        ``DeepResearchSearchClient`` is reused by the lightweight search
        service, so context is refreshed per call rather than captured once at
        process startup.  Existing callers that omit all fields retain their
        previous behaviour while still inheriting TurnContext policy.
        """

        if user_id is not None:
            self.user_id = str(user_id or "")
        if session_id is not None:
            self.session_id = str(session_id or "")
        if project_id is not None:
            self.project_id = str(project_id or "") or None
        if session_context is not None:
            self.session_context = dict(session_context)
        if project_metadata is not None:
            self.project_metadata = dict(project_metadata)
        if self.project_id:
            self.project_metadata = dict(self.project_metadata or {})
            self.project_metadata.setdefault("project_id", self.project_id)
        old_identity = (
            self._privacy_gateway.user_id,
            self._privacy_gateway.session_id,
        )
        new_identity = (self.user_id, self.session_id)
        if old_identity != new_identity:
            self._privacy_gateway._raw_to_alias.clear()
            self._privacy_gateway._alias_to_raw.clear()
            self._privacy_gateway._counters.clear()
        self._privacy_gateway.user_id, self._privacy_gateway.session_id = new_identity
        self._privacy_gateway.update_policy_context(
            session_context=self.session_context,
            project_metadata=self.project_metadata,
        )

    @staticmethod
    def _external_base_url(provider: str) -> str:
        # The privacy gateway classifies the provider id as external. Keeping
        # a concrete URL makes the audit record and local-only preflight
        # unambiguous without making an HTTP request.
        return {
            "duckduckgo": "https://html.duckduckgo.com/html/",
            "yahoo_realtime": DEFAULT_YAHOO_REALTIME_URL,
            "wikipedia": "https://en.wikipedia.org/w/api.php",
            "arxiv": "https://export.arxiv.org/api/query",
            "openalex": "https://api.openalex.org/works",
            "pubmed": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/",
        }.get(provider, "https://example.invalid/")

    @staticmethod
    def _search_descriptor(
        *,
        action: str,
        transport: str,
        destination: str,
        provider: str,
        tool: str = "deep_research",
        model: str = "",
    ) -> EgressDescriptor:
        """Describe one concrete deep-research provider transaction.

        The descriptor is intentionally created at the call site for every
        request (including language fan-out and PubMed summary calls).  This
        keeps review/audit binding scoped to the exact route and means a
        retry cannot accidentally reuse an approval for another payload.
        """

        return EgressDescriptor(
            action=str(action or "deep_research_search"),
            transport=str(transport or "httpx.AsyncClient.get"),
            destination=str(destination or ""),
            provider=str(provider or ""),
            tool=str(tool or "deep_research"),
            model=str(model or ""),
        )

    @staticmethod
    async def _http_get(
        client: Any,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """Call an injected/real HTTP client with redirects disabled.

        A small compatibility fallback is retained for test doubles that do
        not accept ``follow_redirects``; real ``httpx`` clients always receive
        the explicit false value and never follow provider redirects.
        """

        getter = getattr(client, "get", None)
        if not callable(getter):
            raise PrivacyError("search client does not expose get()")
        kwargs: dict[str, Any] = {
            "params": dict(params or {}),
            "follow_redirects": False,
        }
        if headers:
            kwargs["headers"] = dict(headers)
        try:
            response = getter(url, **kwargs)
        except TypeError:
            # Tiny fixture clients often implement only ``get(url, params=)``.
            # This path is never used by httpx and therefore cannot weaken
            # redirect policy for a production transport.
            kwargs.pop("follow_redirects", None)
            response = getter(url, **kwargs)
        if hasattr(response, "__await__"):
            return await response
        return response

    async def _execute_http_get(
        self,
        client: Any,
        *,
        url: str,
        payload: Mapping[str, Any],
        provider: str,
        source_kind: str,
        params_builder: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        headers: Mapping[str, str] | None = None,
        action: str | None = None,
        model: str = "",
    ) -> Any:
        """Review/mask one request, then invoke its GET sender exactly once."""

        execute = getattr(self._privacy_gateway, "execute", None)
        if not callable(execute):
            # Never fall back to ``protect`` + raw transport.  An injected
            # legacy gateway lacking the transaction API must fail closed.
            raise PrivacyError("outbound privacy gateway does not support execution")

        descriptor = self._search_descriptor(
            action=action or source_kind,
            transport="httpx.AsyncClient.get",
            destination=url,
            provider=provider,
            model=model,
        )

        async def send(final_payload: Any) -> Any:
            if not isinstance(final_payload, Mapping):
                raise PrivacyError("search privacy boundary returned no mapping payload")
            params = params_builder(final_payload)
            if not isinstance(params, Mapping):
                raise PrivacyError("search request parameter builder returned no mapping")
            return await self._http_get(
                client,
                url,
                params=params,
                headers=headers,
            )

        return await execute(
            dict(payload),
            provider=provider,
            descriptor=descriptor,
            sender=send,
            base_url=url,
            source_kind=source_kind,
            model=model,
        )

    @staticmethod
    def _protected_query_value(payload: Mapping[str, Any]) -> str:
        """Extract the gateway-approved query without raw fallback."""

        value = payload.get("query")
        if not isinstance(value, str) or not value.strip():
            raise PrivacyError("privacy boundary returned no protected query")
        return value

    def _request_scope(
        self,
        *,
        user_id: Optional[str],
        actor_user_id: Optional[str],
        session_id: Optional[str],
        project_id: Optional[str],
        session_context: Optional[Mapping[str, Any]],
        project_metadata: Optional[Mapping[str, Any]],
    ) -> "DeepResearchSearchClient":
        """Return an isolated client view for one concurrent search job.

        ``DeepResearchSearchClient`` is shared by quick-search and queued
        research jobs.  The previous implementation mutated ``self`` while
        ``asyncio.gather`` was running, so one user's gateway/alias map could
        be replaced by another user's scope.  A shallow copy safely shares
        immutable configuration and timeout settings while giving each job a
        private identity, metadata, and gateway.
        """

        scoped = copy.copy(self)
        scoped.user_id = str(user_id or actor_user_id or "")
        scoped.session_id = str(session_id or "")
        scoped.project_id = str(project_id or "") or None
        scoped.session_context = (
            dict(session_context) if isinstance(session_context, Mapping) else None
        )
        scoped.project_metadata = (
            dict(project_metadata) if isinstance(project_metadata, Mapping) else None
        )
        if scoped.project_id:
            scoped.project_metadata = dict(scoped.project_metadata or {})
            scoped.project_metadata.setdefault("project_id", scoped.project_id)
        scoped._privacy_gateway = OutboundPrivacyGateway(
            scoped.config,
            user_id=scoped.user_id,
            session_id=scoped.session_id,
            session_context=scoped.session_context,
            project_metadata=scoped.project_metadata,
        )
        return scoped

    def _observe_engine(
        self,
        engine: str,
        *,
        success: bool,
        reason: Optional[str] = None,
    ) -> None:
        normalized = str(engine or "").strip().lower()
        if not normalized:
            return
        if success:
            safe_reason = "transport_succeeded"
        else:
            # Provider exception text can contain credentials, proxy URLs or
            # internal hostnames.  Keep only a stable machine-readable reason.
            if isinstance(reason, str) and "privacy" in reason.lower():
                safe_reason = "privacy_blocked"
            elif isinstance(reason, str) and "timeout" in reason.lower():
                safe_reason = "timeout"
            elif isinstance(reason, str) and "connect" in reason.lower():
                safe_reason = "unreachable"
            else:
                safe_reason = "transport_failed"
        self._engine_observations[normalized] = {
            "reachability": "ready" if success else "unreachable",
            "reason": safe_reason,
            "checked_at": _utc_now(),
        }

    async def search(
        self,
        query: str,
        *,
        engines: Iterable[str],
        max_results_per_engine: int,
        project_id: Optional[str] = None,
        include_local_knowledge: bool = False,
        actor_user_id: Optional[str] = None,
        is_admin: bool = False,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        session_context: Optional[Mapping[str, Any]] = None,
        project_metadata: Optional[Mapping[str, Any]] = None,
    ) -> list[DeepResearchSource]:
        # Never mutate the shared search client while concurrent jobs are in
        # flight.  All provider methods below run on this request-local copy.
        scoped = self._request_scope(
            user_id=user_id,
            actor_user_id=actor_user_id,
            session_id=session_id,
            project_id=project_id,
            session_context=session_context,
            project_metadata=project_metadata,
        )
        return await scoped._search_bound(
            query,
            engines=engines,
            max_results_per_engine=max_results_per_engine,
            project_id=project_id,
            include_local_knowledge=include_local_knowledge,
            actor_user_id=actor_user_id,
            is_admin=is_admin,
            user_id=user_id,
            session_id=session_id,
            session_context=session_context,
            project_metadata=project_metadata,
        )

    async def _search_bound(
        self,
        query: str,
        *,
        engines: Iterable[str],
        max_results_per_engine: int,
        project_id: Optional[str] = None,
        include_local_knowledge: bool = False,
        actor_user_id: Optional[str] = None,
        is_admin: bool = False,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        session_context: Optional[Mapping[str, Any]] = None,
        project_metadata: Optional[Mapping[str, Any]] = None,
    ) -> list[DeepResearchSource]:
        # ``search`` is the request boundary; clear a previous caller's
        # identity when this invocation omits optional scope instead of
        # allowing aliases/project bindings to bleed across jobs.
        self.user_id = str(user_id or actor_user_id or "")
        self.session_id = str(session_id or "")
        self.project_id = str(project_id or "") or None
        self.session_context = (
            dict(session_context) if isinstance(session_context, Mapping) else None
        )
        self.project_metadata = (
            dict(project_metadata) if isinstance(project_metadata, Mapping) else None
        )
        if self.project_id:
            self.project_metadata = dict(self.project_metadata or {})
            self.project_metadata.setdefault("project_id", self.project_id)
        old_identity = (
            self._privacy_gateway.user_id,
            self._privacy_gateway.session_id,
        )
        new_identity = (self.user_id, self.session_id)
        if old_identity != new_identity:
            self._privacy_gateway._raw_to_alias.clear()
            self._privacy_gateway._alias_to_raw.clear()
            self._privacy_gateway._counters.clear()
        self._privacy_gateway.user_id, self._privacy_gateway.session_id = new_identity
        self._privacy_gateway.update_policy_context(
            session_context=self.session_context,
            project_metadata=self.project_metadata,
        )
        tasks: list[tuple[str, Awaitable[list[DeepResearchSource]]]] = []
        selected = [_normalize_engine_id(engine) for engine in engines]
        unknown_engines = sorted(set(selected) - SUPPORTED_ENGINES)
        if unknown_engines:
            # Do not let an unrecognized identifier fall through to an empty
            # task list and get reported as an egress outage.  The exact
            # values are provider-controlled request data; expose only the
            # stable provider_invalid code to the job/route.
            raise DeepResearchProviderError("provider_invalid")
        # Fail closed before opening an AsyncClient or scheduling any external
        # search transport.  In particular, local_only must not degrade to an
        # empty result set that looks like a successful search.
        searxng_url = self._searxng_url()
        if "searxng" in selected and searxng_url:
            if is_enterprise_profile():
                try:
                    # A configured SearXNG label does not make an arbitrary
                    # URL local.  Require loopback/explicit trusted-host
                    # classification (or a deliberate public-egress flag)
                    # before opening the shared HTTP client.
                    assert_public_search_egress_approved(
                        self.config,
                        engine="searxng",
                        endpoint=searxng_url,
                    )
                except SearchEgressPreconditionError as exc:
                    raise DeepResearchTransportError("egress_unreachable") from exc
            self._privacy_gateway.ensure_provider_allowed(
                "openai_compatible_local",
                base_url=searxng_url,
            )
        external_engines = {
            "duckduckgo",
            "yahoo_realtime",
            "wikipedia",
            "arxiv",
            "openalex",
            "pubmed",
        }
        enterprise = is_enterprise_profile()
        yahoo_url = self._yahoo_realtime_url()
        yahoo_intent_completed = False
        # X links/posts have a substantially more reliable source in Yahoo's
        # realtime index than generic web engines.  Always put that request
        # first for an X-intent query, and keep it out of the subsequent
        # gather so Yahoo and SearXNG (or another provider) cannot start at
        # the same time.  If the caller did not explicitly select Yahoo, the
        # intent still opts it in when the endpoint is available; this keeps
        # direct URL/post research deterministic without changing ordinary
        # query behaviour.
        x_intent = self._is_x_intent_query(query)
        if x_intent and yahoo_url and "yahoo_realtime" not in selected:
            selected.insert(0, "yahoo_realtime")
        if "yahoo_realtime" in selected and yahoo_url:
            if enterprise:
                try:
                    assert_public_search_egress_approved(
                        self.config,
                        engine="yahoo_realtime",
                        endpoint=yahoo_url,
                    )
                except SearchEgressPreconditionError as exc:
                    raise DeepResearchTransportError("egress_unreachable") from exc
            self._privacy_gateway.ensure_provider_allowed(
                "yahoo_realtime",
                base_url=yahoo_url,
            )
        for engine in set(selected).intersection(external_engines):
            if engine == "yahoo_realtime":
                # The Yahoo endpoint is checked above using the configured
                # URL.  Keep the provider allowlist explicit even when the
                # caller supplied an alias in ``engines``.
                continue
            if enterprise:
                try:
                    assert_public_search_egress_approved(
                        self.config,
                        engine=engine,
                        endpoint=self._external_base_url(engine),
                    )
                except SearchEgressPreconditionError as exc:
                    raise DeepResearchTransportError("egress_unreachable") from exc
            self._privacy_gateway.ensure_provider_allowed(
                "openai",
                base_url=self._external_base_url(engine),
            )
        transport_errors: list[BaseException] = []
        provider_errors: list[BaseException] = []
        # A redirect can move a protected query to an arbitrary host after
        # provider preflight has completed.  Keep the destination bound to the
        # configured provider and let each adapter surface 3xx as a typed
        # failure instead of following it automatically.
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
            batches: list[Any] = []
            if x_intent and "yahoo_realtime" in selected and yahoo_url:
                try:
                    # This await is intentional; do not move Yahoo into the
                    # gather below.  URL/post evidence must settle before
                    # generic search providers can race it.
                    batches.append(
                        await self._search_yahoo_realtime(
                            client, query, max_results_per_engine
                        )
                    )
                    yahoo_intent_completed = True
                    self._observe_engine("yahoo_realtime", success=True)
                except (ExternalProviderBlocked, PrivacyError):
                    raise
                except Exception as exc:  # provider failure must not hide others
                    self._observe_engine("yahoo_realtime", success=False, reason=str(exc))
                    if _is_transport_failure(exc):
                        transport_errors.append(exc)
                    else:
                        provider_errors.append(exc)
                    logger.debug(
                        "Yahoo realtime search failed (%s)",
                        getattr(exc, "code", type(exc).__name__),
                    )
            if "searxng" in selected:
                if self._searxng_url():
                    tasks.append(
                        ("searxng", self._search_searxng(client, query, max_results_per_engine))
                    )
                elif not enterprise and "duckduckgo" not in selected:
                    tasks.append(
                        ("duckduckgo", self._search_duckduckgo(client, query, max_results_per_engine))
                    )
            if "duckduckgo" in selected:
                tasks.append(
                    ("duckduckgo", self._search_duckduckgo(client, query, max_results_per_engine))
                )
            if "yahoo_realtime" in selected and not (x_intent and yahoo_url):
                if yahoo_url:
                    tasks.append(
                        (
                            "yahoo_realtime",
                            self._search_yahoo_realtime(
                                client, query, max_results_per_engine
                            ),
                        )
                    )
            if "wikipedia" in selected:
                tasks.append(
                    ("wikipedia", self._search_wikipedia(client, query, max_results_per_engine))
                )
            if "arxiv" in selected:
                tasks.append(("arxiv", self._search_arxiv(client, query, max_results_per_engine)))
            if "openalex" in selected:
                tasks.append(
                    ("openalex", self._search_openalex(client, query, max_results_per_engine))
                )
            if "pubmed" in selected:
                tasks.append(("pubmed", self._search_pubmed(client, query, max_results_per_engine)))
            if include_local_knowledge:
                tasks.append(
                    (
                        "local_knowledge",
                        self._search_local_knowledge(
                            query,
                            max_results_per_engine,
                            project_id,
                            actor_user_id=actor_user_id,
                            is_admin=is_admin,
                        ),
                    )
                )

            # An Enterprise request with no configured internal route must
            # fail truthfully before the empty result can be mistaken for a
            # successful search.  Personal callers retain the historical
            # empty-result compatibility path.
            if (
                enterprise
                and not tasks
                and not include_local_knowledge
                and not yahoo_intent_completed
            ):
                raise DeepResearchTransportError("egress_unreachable")

            batches.extend(
                await asyncio.gather(
                    *[task for _engine, task in tasks],
                    return_exceptions=True,
                )
            )

        results: list[DeepResearchSource] = []
        successful_batches = 0
        successful_external_batches = 1 if yahoo_intent_completed else 0
        # The first ``batches`` entries may include the serialized X-intent
        # Yahoo result above.  Mark named tasks by their positional result and
        # leave preflight/privacy failures as unobserved (no transport ran).
        named_offset = 1 if yahoo_intent_completed else 0
        for index, batch in enumerate(batches):
            if isinstance(batch, BaseException):
                if isinstance(batch, asyncio.CancelledError):
                    raise batch
                if isinstance(batch, (ExternalProviderBlocked, PrivacyError)):
                    raise batch
                if isinstance(
                    batch,
                    (
                        httpx.TimeoutException,
                        asyncio.TimeoutError,
                        httpx.ConnectError,
                        httpx.NetworkError,
                        httpx.HTTPStatusError,
                        OSError,
                    ),
                ):
                    transport_errors.append(batch)
                elif isinstance(batch, DeepResearchTransportError):
                    transport_errors.append(batch)
                else:
                    provider_errors.append(batch)
                task_index = index - named_offset
                if 0 <= task_index < len(tasks):
                    self._observe_engine(
                        tasks[task_index][0], success=False, reason=str(batch)
                    )
                logger.debug(
                    "Deep research search batch failed (%s)",
                    getattr(batch, "code", type(batch).__name__),
                )
                continue
            task_index = index - named_offset
            if 0 <= task_index < len(tasks):
                self._observe_engine(tasks[task_index][0], success=True)
                if tasks[task_index][0] in external_engines or tasks[task_index][0] == "searxng":
                    # An empty list is still a successful provider transport;
                    # it must count only for the external engine that actually
                    # answered, not for a local-knowledge task that could
                    # otherwise mask a complete public-egress outage.
                    successful_external_batches += 1
            successful_batches += 1
            results.extend(batch)
        external_requested = bool(
            set(selected).intersection(external_engines | {"searxng"})
        )
        if enterprise and external_requested and successful_external_batches == 0 and transport_errors:
            if any(
                isinstance(error, (asyncio.TimeoutError, httpx.TimeoutException))
                or getattr(error, "code", None) == "engine_timeout"
                for error in transport_errors
            ):
                raise DeepResearchTransportError("engine_timeout")
            raise DeepResearchTransportError("egress_unreachable")
        if enterprise and external_requested and successful_external_batches == 0 and provider_errors:
            raise DeepResearchProviderError()
        if enterprise and external_requested and successful_external_batches == 0:
            # Local-knowledge results must not mask an unavailable external
            # route.  A request that explicitly selected SearXNG/Yahoo/etc.
            # is only successful after at least one external transport has
            # answered, even when the local knowledge task returned rows.
            raise DeepResearchTransportError("egress_unreachable")
        return self._dedupe(results)

    def available_engines(self) -> list[dict[str, Any]]:
        searxng_url = self._searxng_url()
        configured = {
            "searxng": bool(searxng_url),
            "yahoo_realtime": bool(self._yahoo_realtime_url()),
            "duckduckgo": True,
            "wikipedia": True,
            "arxiv": True,
            "openalex": True,
            "pubmed": True,
        }
        labels = {
            "searxng": "SearXNG",
            "yahoo_realtime": "Yahoo!リアルタイム検索",
            "duckduckgo": "DuckDuckGo HTML",
            "wikipedia": "Wikipedia",
            "arxiv": "arXiv",
            "openalex": "OpenAlex",
            "pubmed": "PubMed",
        }
        entries: list[dict[str, Any]] = []
        for engine, is_configured in configured.items():
            observation = self._engine_observations.get(engine)
            reachability = (
                str(observation.get("reachability"))
                if observation
                else "unknown"
            )
            reason = (
                str(observation.get("reason"))
                if observation
                else ("not_configured" if not is_configured else "not_checked")
            )
            entries.append(
                {
                    "id": engine,
                    "label": labels[engine],
                    # ``available`` is retained for older clients, but it is
                    # derived from an observed successful transport rather
                    # than registration/configuration alone.
                    "available": bool(
                        is_configured and observation and reachability == "ready"
                    ),
                    "configured": is_configured,
                    "reachability": reachability,
                    "reason": reason,
                    "checked_at": observation.get("checked_at") if observation else None,
                }
            )
        return entries

    def _searxng_url(self) -> str:
        configured = (
            os.getenv("AOITALK_DEEP_RESEARCH_SEARXNG_URL")
            or _config_get(self.config, "deep_research.searxng_url", "")
            or _config_get(self.config, "search.searxng_url", "")
        )
        return str(configured).rstrip("/") if configured else ""

    def _yahoo_realtime_url(self) -> str:
        """Return the configured Yahoo! realtime search endpoint.

        The public Yahoo endpoint is the safe default.  A deployment may
        point at a same-contract proxy (for example, for egress auditing) via
        the explicit URL setting; the outbound privacy gateway still owns the
        local-only/external decision immediately before transport.
        """

        configured = (
            os.getenv("AOITALK_DEEP_RESEARCH_YAHOO_REALTIME_URL")
            or os.getenv("AOITALK_YAHOO_REALTIME_URL")
            or _config_get(self.config, "deep_research.yahoo_realtime_url", "")
            or _config_get(self.config, "search.yahoo_realtime_url", "")
        )
        return str(configured or DEFAULT_YAHOO_REALTIME_URL).rstrip("/")

    @staticmethod
    def _is_x_intent_query(query: str) -> bool:
        """Return whether a query is asking for X/Twitter post evidence.

        URL-shaped status references are unambiguous.  The token checks cover
        natural-language requests (Japanese and English) while deliberately
        avoiding a bare single-letter ``x`` match.
        """

        value = str(query or "").strip()
        if not value:
            return False
        # A direct status URL is always an X intent, even when it is supplied
        # without an imperative verb (the URL-ingest caller uses this shape).
        try:
            from .yahoo_realtime_search_service import x_status_id

            if any(x_status_id(token.rstrip(".,。！？!?")) for token in re.findall(r"https?://[^\s<>]+", value)):
                return True
            from .yahoo_realtime_search_service import looks_like_x_search_request

            return bool(looks_like_x_search_request(value))
        except Exception:
            # Keep search usable when the optional Yahoo parser is unavailable
            # during a partial installation; only unambiguous URL forms are
            # accepted by this fallback.
            return bool(
                re.search(
                    r"https?://(?:www\.|mobile\.)?(?:x\.com|twitter\.com)/[^\s?#]*/?(?:status|statuses)/\d+",
                    value,
                    flags=re.IGNORECASE,
                )
            )

    async def _search_duckduckgo(
        self, client: httpx.AsyncClient, query: str, limit: int
    ) -> list[DeepResearchSource]:
        endpoint = self._external_base_url("duckduckgo")
        response = await self._execute_http_get(
            client,
            url=endpoint,
            payload={"query": str(query or "")},
            provider="duckduckgo",
            source_kind="deep_research_search_duckduckgo",
            params_builder=lambda protected: {
                "q": self._protected_query_value(protected),
                "kl": "jp-jp",
            },
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; AoiTalkLocalSearch/0.1; "
                    "+https://github.com/ttttdiva/41_AoiTalk)"
                )
            },
        )
        response.raise_for_status()
        text = response.text
        sources: list[DeepResearchSource] = []
        matches = list(
            re.finditer(
                r'<a[^>]+class="[^"]*\bresult__a\b[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                text,
                flags=re.IGNORECASE | re.DOTALL,
            )
        )
        for index, match in enumerate(matches[:limit]):
            href = _normalize_duckduckgo_url(html.unescape(match.group(1)))
            title = _strip_html(match.group(2))
            if not title:
                continue
            block_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            block = text[match.end() : block_end]
            snippet_match = re.search(
                r'class="[^"]*\bresult__snippet\b[^"]*"[^>]*>(.*?)</(?:a|div)>',
                block,
                flags=re.IGNORECASE | re.DOTALL,
            )
            sources.append(
                DeepResearchSource(
                    id=0,
                    title=title,
                    url=href,
                    snippet=_strip_html(snippet_match.group(1) if snippet_match else ""),
                    engine="duckduckgo",
                    query=query,
                )
            )
        return sources

    async def search_yahoo_realtime(
        self,
        client: httpx.AsyncClient,
        query: str,
        limit: int = 5,
    ) -> list[DeepResearchSource]:
        """Delegate Yahoo transport/parsing to the shared search service."""

        from .yahoo_realtime_search_service import search_yahoo_realtime

        posts_result = await search_yahoo_realtime(
            client,
            query,
            limit=limit,
            privacy_gateway=self._privacy_gateway,
            base_url=self._yahoo_realtime_url(),
        )
        result_status = str(
            getattr(posts_result, "status", "")
            if not isinstance(posts_result, Mapping)
            else posts_result.get("status", "")
        ).strip().lower()
        if result_status in {"blocked", "privacy_blocked"}:
            raise ExternalProviderBlocked(
                "Yahoo realtime search was blocked by the privacy policy"
            )
        if result_status in {
            "timeout",
            "invalid_endpoint",
            "network_error",
            "http_error",
            "redirect_rejected",
            "body_too_large",
            "parse_error",
        }:
            # The Yahoo adapter returns typed failure envelopes rather than
            # raising transport exceptions.  Propagate them here so the
            # engine observation cannot mark a failed probe as ``ready`` and
            # the runner can persist a truthful terminal failure.
            raise DeepResearchTransportError(
                "engine_timeout" if result_status == "timeout" else "egress_unreachable"
            )
        # The shared service returns a typed result envelope.  Accepting a
        # plain list/mapping as well keeps this boundary compatible with small
        # test doubles and older embedders without reintroducing a parser.
        if hasattr(posts_result, "posts"):
            posts = list(getattr(posts_result, "posts", ()) or ())
        elif isinstance(posts_result, Mapping):
            posts = posts_result.get("posts") or posts_result.get("results") or []
        else:
            posts = list(posts_result or ())
        sources: list[DeepResearchSource] = []
        for post in posts:
            if isinstance(post, Mapping):
                url = str(post.get("url") or "")
                title = str(post.get("title") or "")
                text = str(post.get("text") or post.get("body") or post.get("snippet") or "")
                author = str(
                    post.get("author")
                    or post.get("author_name")
                    or post.get("author_handle")
                    or ""
                )
                published = str(post.get("published_at") or "")
                raw = dict(post)
            else:
                url = str(getattr(post, "url", "") or "")
                title = str(getattr(post, "title", "") or "")
                text = str(
                    getattr(post, "text", "")
                    or getattr(post, "body", "")
                    or getattr(post, "snippet", "")
                    or ""
                )
                author = str(
                    getattr(post, "author", "")
                    or getattr(post, "author_name", "")
                    or getattr(post, "author_handle", "")
                    or ""
                )
                published = str(getattr(post, "published_at", "") or "")
                raw = dict(getattr(post, "raw", {}) or {})
            if not url:
                continue
            raw.setdefault("author", author)
            raw.setdefault("text", text)
            raw.setdefault("published_at", published)
            sources.append(
                DeepResearchSource(
                    id=0,
                    title=title or text[:120] or url,
                    url=url,
                    snippet=text,
                    engine="yahoo-realtime",
                    query=query,
                    published_at=published or None,
                    raw=raw,
                )
            )
        return sources[: max(1, int(limit or 1))]

    async def _search_yahoo_realtime(
        self, client: httpx.AsyncClient, query: str, limit: int
    ) -> list[DeepResearchSource]:
        return await self.search_yahoo_realtime(client, query, limit)

    async def _search_searxng(
        self, client: httpx.AsyncClient, query: str, limit: int
    ) -> list[DeepResearchSource]:
        base_url = self._searxng_url()
        if not base_url:
            return []
        endpoint = f"{base_url}/search"
        response = await self._execute_http_get(
            client,
            url=endpoint,
            payload={"query": str(query or "")},
            provider="openai_compatible_local",
            source_kind="deep_research_search_searxng",
            params_builder=lambda protected: {
                "q": self._protected_query_value(protected),
                "format": "json",
                "language": "ja-JP",
                "safesearch": 1,
            },
        )
        response.raise_for_status()
        data = response.json()
        sources = []
        for item in data.get("results", [])[:limit]:
            url = str(item.get("url") or "")
            title = _strip_html(str(item.get("title") or url or query))
            if not title:
                continue
            sources.append(
                DeepResearchSource(
                    id=0,
                    title=title,
                    url=url,
                    snippet=_strip_html(str(item.get("content") or "")),
                    engine="searxng",
                    query=query,
                    raw={"score": item.get("score")},
                )
            )
        return sources

    async def _search_wikipedia(
        self, client: httpx.AsyncClient, query: str, limit: int
    ) -> list[DeepResearchSource]:
        async def run_language(lang: str) -> list[DeepResearchSource]:
            endpoint = f"https://{lang}.wikipedia.org/w/api.php"
            response = await self._execute_http_get(
                client,
                url=endpoint,
                payload={"query": str(query or "")},
                provider="wikipedia",
                source_kind="deep_research_search_wikipedia",
                action=f"deep_research_search_wikipedia_{lang}",
                params_builder=lambda protected: {
                    "action": "query",
                    "list": "search",
                    "srsearch": self._protected_query_value(protected),
                    "format": "json",
                    "srlimit": limit,
                    "utf8": 1,
                },
                headers={
                    "User-Agent": (
                        "AoiTalkDeepResearch/0.1 "
                        "(https://github.com/ttttdiva/41_AoiTalk; local-search)"
                    )
                },
            )
            response.raise_for_status()
            sources = []
            for item in response.json().get("query", {}).get("search", []):
                title = str(item.get("title") or "")
                if not title:
                    continue
                sources.append(
                    DeepResearchSource(
                        id=0,
                        title=title,
                        url=f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}",
                        snippet=_strip_html(str(item.get("snippet") or "")),
                        engine=f"wikipedia-{lang}",
                        query=query,
                        published_at=str(item.get("timestamp") or "") or None,
                        raw={"pageid": item.get("pageid")},
                    )
                )
            return sources

        batches = await asyncio.gather(
            run_language("ja"), run_language("en"), return_exceptions=True
        )
        sources: list[DeepResearchSource] = []
        failures: list[BaseException] = []
        for batch in batches:
            if isinstance(batch, BaseException):
                if isinstance(batch, asyncio.CancelledError):
                    raise batch
                if isinstance(batch, (ExternalProviderBlocked, PrivacyError)):
                    raise batch
                failures.append(batch)
                continue
            sources.extend(batch)
        # A valid response with zero hits is a successful empty search.  If
        # both language probes failed before producing a response, propagate a
        # representative failure so readiness/lifecycle cannot report
        # ``ready`` or complete a job with a fabricated empty result.
        if not sources and failures:
            raise failures[0]
        return sources[:limit]

    async def _search_arxiv(
        self, client: httpx.AsyncClient, query: str, limit: int
    ) -> list[DeepResearchSource]:
        endpoint = self._external_base_url("arxiv")
        response = await self._execute_http_get(
            client,
            url=endpoint,
            payload={"query": str(query or "")},
            provider="arxiv",
            source_kind="deep_research_search_arxiv",
            params_builder=lambda protected: {
                "search_query": f"all:{self._protected_query_value(protected)}",
                "start": 0,
                "max_results": limit,
            },
        )
        response.raise_for_status()
        root = ET.fromstring(response.text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        sources = []
        for entry in root.findall("atom:entry", ns):
            title = _strip_html(entry.findtext("atom:title", default="", namespaces=ns))
            url = entry.findtext("atom:id", default="", namespaces=ns)
            summary = _strip_html(entry.findtext("atom:summary", default="", namespaces=ns))
            published = entry.findtext("atom:published", default="", namespaces=ns) or None
            if title:
                sources.append(
                    DeepResearchSource(
                        id=0,
                        title=title,
                        url=url,
                        snippet=summary,
                        engine="arxiv",
                        query=query,
                        published_at=published,
                    )
                )
        return sources

    async def _search_openalex(
        self, client: httpx.AsyncClient, query: str, limit: int
    ) -> list[DeepResearchSource]:
        endpoint = self._external_base_url("openalex")
        response = await self._execute_http_get(
            client,
            url=endpoint,
            payload={"query": str(query or "")},
            provider="openalex",
            source_kind="deep_research_search_openalex",
            params_builder=lambda protected: {
                "search": self._protected_query_value(protected),
                "per-page": limit,
                "sort": "relevance_score:desc",
            },
            headers={"User-Agent": "AoiTalkDeepResearch/0.1 (mailto:local@example.invalid)"},
        )
        response.raise_for_status()
        sources = []
        for item in response.json().get("results", []):
            title = str(item.get("title") or "")
            if not title:
                continue
            location = item.get("primary_location") or {}
            landing = location.get("landing_page_url") if isinstance(location, dict) else None
            url = landing or item.get("doi") or item.get("id") or ""
            sources.append(
                DeepResearchSource(
                    id=0,
                    title=title,
                    url=str(url),
                    snippet=_openalex_abstract(item.get("abstract_inverted_index")),
                    engine="openalex",
                    query=query,
                    published_at=str(item.get("publication_date") or "") or None,
                    raw={"cited_by_count": item.get("cited_by_count")},
                )
            )
        return sources

    async def _search_pubmed(
        self, client: httpx.AsyncClient, query: str, limit: int
    ) -> list[DeepResearchSource]:
        search_endpoint = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        response_headers = {"User-Agent": "AoiTalkDeepResearch/0.1"}
        search = await self._execute_http_get(
            client,
            url=search_endpoint,
            payload={"query": str(query or "")},
            provider="pubmed",
            source_kind="deep_research_search_pubmed",
            params_builder=lambda protected: {
                "db": "pubmed",
                "term": self._protected_query_value(protected),
                "retmode": "json",
                "retmax": limit,
            },
            headers=response_headers,
        )
        search.raise_for_status()
        ids = search.json().get("esearchresult", {}).get("idlist", [])
        if not ids:
            return []
        summary_endpoint = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
        summary = await self._execute_http_get(
            client,
            url=summary_endpoint,
            payload={
                "db": "pubmed",
                "id": ",".join(str(item) for item in ids),
                "retmode": "json",
            },
            provider="pubmed",
            source_kind="deep_research_search_pubmed_summary",
            action="deep_research_search_pubmed_summary",
            params_builder=lambda protected: {
                "db": str(protected.get("db") or "pubmed"),
                "id": str(protected.get("id") or ",".join(str(item) for item in ids)),
                "retmode": str(protected.get("retmode") or "json"),
            },
            headers=response_headers,
        )
        summary.raise_for_status()
        data = summary.json().get("result", {})
        sources = []
        for pmid in ids:
            item = data.get(str(pmid), {})
            title = str(item.get("title") or "")
            if not title:
                continue
            journal = item.get("fulljournalname") or item.get("source") or "PubMed"
            pubdate = item.get("pubdate") or None
            sources.append(
                DeepResearchSource(
                    id=0,
                    title=_strip_html(title),
                    url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                    snippet=_strip_html(f"{journal}. {pubdate or ''}".strip()),
                    engine="pubmed",
                    query=query,
                    published_at=str(pubdate) if pubdate else None,
                    raw={"pmid": pmid},
                )
            )
        return sources

    async def _search_local_knowledge(
        self,
        query: str,
        limit: int,
        project_id: Optional[str],
        *,
        actor_user_id: Optional[str],
        is_admin: bool,
    ) -> list[DeepResearchSource]:
        try:
            from ..knowledge.service import KnowledgeSearchFilters, KnowledgeService
            from ..memory.database import get_database_manager

            db = get_database_manager()
            session = await db.get_session()
            actor_uuid = uuid.UUID(actor_user_id) if actor_user_id else None
            try:
                knowledge_results = await KnowledgeService.search(
                    session,
                    query=query,
                    actor_user_id=actor_uuid,
                    is_admin=is_admin,
                    filters=KnowledgeSearchFilters(
                        project_id=uuid.UUID(project_id) if project_id else None
                    ),
                    limit=limit,
                )
            finally:
                await session.close()
            results = [
                {
                    "text": item["chunk"]["text"],
                    "document": item["document"],
                    "source": item["source"],
                }
                for item in knowledge_results
            ]
        except Exception as exc:
            logger.debug("Local Knowledge search skipped: %s", exc)
            return []

        sources = []
        for index, item in enumerate(results[:limit], start=1):
            text = str(item.get("text") or item.get("content") or item)
            sources.append(
                DeepResearchSource(
                    id=0,
                    title=f"Local Knowledge result {index}",
                    url="",
                    snippet=_strip_html(text),
                    engine="local-knowledge",
                    query=query,
                    raw=item if isinstance(item, dict) else {},
                )
            )
        return sources

    def _dedupe(self, sources: Iterable[DeepResearchSource]) -> list[DeepResearchSource]:
        seen: set[str] = set()
        deduped: list[DeepResearchSource] = []
        for source in sources:
            key = _source_key(source)
            if not key or key in seen:
                continue
            seen.add(key)
            source.id = len(deduped) + 1
            source.snippet = _truncate(source.snippet, 1200)
            deduped.append(source)
        return deduped


def _openalex_abstract(index: Any) -> str:
    if not isinstance(index, dict):
        return ""
    words: list[tuple[int, str]] = []
    for word, positions in index.items():
        if not isinstance(positions, list):
            continue
        for pos in positions:
            try:
                words.append((int(pos), str(word)))
            except Exception:
                continue
    return " ".join(word for _, word in sorted(words))


class DeepResearchRunner:
    def __init__(
        self,
        *,
        config: Any,
        store: DeepResearchJobStore,
        search_client: Optional[DeepResearchSearchClient] = None,
        llm_factory: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.config = config
        self.store = store
        self.search_client = search_client or DeepResearchSearchClient(config)
        self.llm_factory = llm_factory or (lambda user_id: DeepResearchLLMAdapter(config, user_id))

    @staticmethod
    def _is_enterprise() -> bool:
        try:
            from ..features import Features

            return bool(Features.is_enterprise())
        except Exception:
            return str(os.getenv("AOITALK_PROFILE") or "").strip().lower() == "enterprise" or str(
                os.getenv("AIVTUBER_ENV") or ""
            ).strip().lower() == "enterprise"

    def _timeout(self, stage: str) -> float:
        key = {
            "planning": "planning_timeout_seconds",
            "engine": "engine_timeout_seconds",
            "synthesis": "synthesis_timeout_seconds",
            "overall": "overall_timeout_seconds",
        }[stage]
        default = {
            "planning": DEFAULT_PLANNING_TIMEOUT_SECONDS,
            "engine": DEFAULT_ENGINE_TIMEOUT_SECONDS,
            "synthesis": DEFAULT_SYNTHESIS_TIMEOUT_SECONDS,
            "overall": DEFAULT_OVERALL_TIMEOUT_SECONDS,
        }[stage]
        try:
            value = float(_config_get(self.config, f"deep_research.{key}", default))
        except (TypeError, ValueError):
            value = default
        return max(0.1, min(value, 3600.0))

    async def _current_project_privacy_policy(
        self,
        project_id: Optional[str],
        *,
        conversation: Any = None,
    ) -> dict[str, Any]:
        """Read current project policy from the ACL authority, not the job."""

        if not project_id:
            return {}
        relationship = (
            conversation.get("project")
            if isinstance(conversation, Mapping)
            else None
        )
        raw_metadata = (
            relationship.get("project_metadata")
            if isinstance(relationship, Mapping)
            else getattr(relationship, "project_metadata", None)
        )
        if isinstance(raw_metadata, Mapping):
            return _normalize_privacy_policy(raw_metadata, project_id=project_id)

        # ConversationRepository normally does not eager-load ``project``;
        # query the project ACL/session explicitly and fail closed if the
        # authoritative row cannot be read.
        try:
            from ..memory.database import get_database_manager
            from ..memory.project_repository import ProjectRepository

            database = get_database_manager()
            if database is None:
                raise RuntimeError("database unavailable")
            db_session = await database.get_session()
            try:
                project_uuid = uuid.UUID(str(project_id))
                getter = getattr(ProjectRepository, "get_by_id", None)
                if callable(getter):
                    project = await getter(db_session, project_uuid)
                else:
                    from sqlalchemy import select
                    from ..memory.models import Project

                    result = await db_session.execute(
                        select(Project).where(Project.id == project_uuid)
                    )
                    project = result.scalar_one_or_none()
            finally:
                await db_session.close()
        except Exception as exc:
            raise DeepResearchScopeError("scope_revoked") from exc
        if project is None:
            raise DeepResearchScopeError("scope_revoked")
        raw_metadata = getattr(project, "project_metadata", None)
        return _normalize_privacy_policy(raw_metadata, project_id=project_id)

    async def _validate_scope(
        self,
        request: DeepResearchRequest,
        *,
        effective_user_id: str,
        effective_session_id: Optional[str],
    ) -> None:
        privacy_mode = effective_privacy_mode(
            self.config,
            session_context=request.session_context,
            project_metadata=request.project_metadata,
        )
        if not effective_session_id:
            # Enterprise and protected transports cannot safely scope a
            # permission cache or reversible privacy aliases to a synthetic
            # job id.  Personal/direct legacy callers remain compatible.
            if self._is_enterprise() or privacy_mode == "protected":
                raise DeepResearchScopeError("scope_missing")
            return
        if not effective_user_id:
            raise DeepResearchScopeError("scope_missing")
        # Enterprise workers and any protected/local-only job re-check the
        # durable conversation ACL after the HTTP request has returned.  A
        # revoked session or changed privacy policy therefore cannot race a
        # queued job into external search/model execution.  Personal/direct
        # callers retain the historical compatibility path.
        # Enterprise always carries a durable session scope.  Personal direct
        # callers may use a synthetic/legacy session id in unit integrations;
        # only revalidate those protected calls when an authoritative policy
        # snapshot is present, while Enterprise remains fail-closed regardless.
        requires_revalidation = self._is_enterprise() or (
            privacy_mode in {"protected", "local_only"}
            and bool(request.session_context or request.project_metadata)
        )
        if not requires_revalidation:
            return
        try:
            from ..memory.conversation_repository import ConversationRepository

            repository = ConversationRepository()
            allowed = await repository.user_has_session_write_access(
                str(effective_session_id), str(effective_user_id)
            )
            if not allowed:
                raise DeepResearchScopeError("scope_revoked")
            # Re-read the session itself on every validation.  ACL membership
            # can remain valid while an administrator moves the conversation
            # to another Project; that drift must revoke this job's durable
            # scope rather than silently widening its data access.
            get_session = getattr(repository, "get_session_by_id", None)
            if not callable(get_session):
                raise DeepResearchScopeError("scope_revoked")
            conversation = await get_session(str(effective_session_id), with_messages=False)
            if conversation is None:
                raise DeepResearchScopeError("scope_revoked")

            if isinstance(conversation, Mapping):
                current_project = conversation.get("project_id")
                current_session_context = conversation.get("context")
            else:
                current_project = getattr(conversation, "project_id", None)
                current_session_context = getattr(conversation, "context", None)
            expected_project = str(request.project_id or "").strip().casefold() or None
            current_project_text = (
                str(current_project).strip().casefold() if current_project else None
            )
            if current_project_text != expected_project:
                raise DeepResearchScopeError("scope_revoked")
            # Compare the allowlisted policy subset captured after the initial
            # ACL check with the current durable rows.  A privacy-mode change
            # while a job is queued/running is a scope revocation; continuing
            # would let the job send data under a policy the user did not
            # authorize at launch time.
            expected_session_policy = _normalize_privacy_policy(
                request.session_context
            )
            current_session_policy = _normalize_privacy_policy(current_session_context)
            if current_session_policy != expected_session_policy:
                raise DeepResearchScopeError("scope_revoked")
            expected_project_policy = _normalize_privacy_policy(
                request.project_metadata,
                project_id=request.project_id,
            )
            current_project_policy = await self._current_project_privacy_policy(
                current_project_text,
                conversation=conversation,
            )
            # Project IDs are stable scope metadata; compare only policy keys
            # so equivalent IDs with different casing do not cause false drift.
            expected_project_policy.pop("project_id", None)
            current_project_policy.pop("project_id", None)
            if current_project_policy != expected_project_policy:
                raise DeepResearchScopeError("scope_revoked")
        except Exception:
            # Never expose repository/provider details to the job or route.
            # Preserve an explicit scope error while normalizing database and
            # malformed-session failures to the same fail-closed result.
            raise DeepResearchScopeError("scope_revoked")

    async def _await_stage(self, awaitable: Awaitable[Any], stage: str) -> Any:
        try:
            return await _await_bounded(awaitable, self._timeout(stage))
        except asyncio.TimeoutError as exc:
            raise DeepResearchStageTimeout(stage) from exc

    def _terminalize(
        self,
        job: DeepResearchJob,
        code: str,
        *,
        phase: str = "failed",
        status: str = "failed",
    ) -> DeepResearchJob:
        normalized = code if code in DEEP_RESEARCH_ERROR_MESSAGES else "internal_error"
        if job.status in TERMINAL_STATUSES:
            return job
        job.status = status
        job.error_code = normalized
        job.error = DEEP_RESEARCH_ERROR_MESSAGES[normalized]
        job.completed_at = _utc_now()
        job.emit(
            DEEP_RESEARCH_ERROR_MESSAGES[normalized],
            job.progress,
            phase,
            {"error_code": normalized},
        )
        self.store.save(job)
        return job

    async def run(self, job: DeepResearchJob, request: DeepResearchRequest) -> DeepResearchJob:
        """Run one job with an overall deadline and cancellation settlement."""

        inner_task: asyncio.Task[DeepResearchJob] | None = None
        try:
            inner_task = asyncio.create_task(self._run_inner(job, request))
            _done, pending = await asyncio.wait(
                {inner_task}, timeout=self._timeout("overall")
            )
            if pending:
                inner_task.cancel()
                inner_task.add_done_callback(_consume_async_task)
                raise asyncio.TimeoutError
            return inner_task.result()
        except asyncio.TimeoutError:
            return self._terminalize(job, "deadline")
        except asyncio.CancelledError:
            if inner_task is not None and not inner_task.done():
                inner_task.cancel()
                inner_task.add_done_callback(_consume_async_task)
            # Cancellation is a terminal business state.  Persist it before
            # allowing the worker task to settle so callers never observe a
            # permanently running job.
            self._terminalize(job, "cancelled", status="cancelled")
            return job

    async def _run_inner(self, job: DeepResearchJob, request: DeepResearchRequest) -> DeepResearchJob:
        request = request.normalized(
            enterprise_public_egress_approved=approved_public_egress(self.config)
        )
        # A queued cancellation may race the worker between ``queue.get`` and
        # task registration.  Re-read the durable record before changing it to
        # ``running`` so a canceled job cannot resurrect and perform provider
        # work after the caller has already observed a terminal state.
        persisted = self.store.load(job.id)
        if persisted is not None:
            job = persisted
        if job.status in TERMINAL_STATUSES:
            return job
        job.status = "running"
        job.started_at = _utc_now()
        job.updated_at = job.started_at
        job.emit("調査を開始しました", 2, "setup", {"mode": request.mode})
        self.store.save(job)

        permission_scope_token = None
        turn_scope_token = None
        privacy_policy_token = None
        try:
            turn = get_turn_context()
            expected_privacy_snapshot = _privacy_snapshot(
                request.session_context,
                request.project_metadata,
                # The job's persisted project binding is authoritative when a
                # caller supplies a mutable request object after enqueue.
                project_id=job.project_id or request.project_id,
                global_policy=_config_get(self.config, "external_model_privacy", {}),
            )
            stored_snapshot = job.privacy_snapshot if isinstance(job.privacy_snapshot, Mapping) else {}
            stored_privacy_snapshot = {
                "session": _normalize_privacy_policy(stored_snapshot.get("session")),
                "project": _normalize_privacy_policy(
                    stored_snapshot.get("project"),
                    project_id=job.project_id or request.project_id,
                ),
                "global": _normalize_privacy_policy(stored_snapshot.get("global")),
            }
            # A persisted job snapshot is server-owned.  If a custom caller
            # mutates the request policy between enqueue and execution, reject
            # it instead of allowing the worker to run under a weaker policy.
            if job.privacy_snapshot and stored_privacy_snapshot != expected_privacy_snapshot:
                raise DeepResearchScopeError("scope_revoked")
            effective_mode = effective_privacy_mode(
                self.config,
                session_context=request.session_context,
                project_metadata=request.project_metadata,
            )
            # Workers are created from a neutral ContextVar context.  Bind the
            # job's server-resolved policy explicitly so custom LLM/search
            # adapters that consult ``get_privacy_policy_context`` cannot
            # inherit a previous request's metadata (or accidentally fall
            # back to an unscoped direct policy).
            privacy_policy_token = set_privacy_policy_context(
                session_context=request.session_context,
                project_metadata=request.project_metadata,
            )
            protected_scope = self._is_enterprise() or effective_mode in {
                "protected",
                "local_only",
            }
            # For protected/Enterprise jobs the request's server-validated
            # scope is authoritative.  Do not inherit a caller ContextVar or
            # fabricate a job-id scope inside a background worker.
            effective_session_id = (
                job.session_id or request.session_id
                if protected_scope
                else request.session_id or job.session_id or turn.session_id
            )
            effective_user_id = (
                job.actor_user_id or request.actor_user_id
                if protected_scope
                else request.actor_user_id or job.actor_user_id or job.user_id
            )
            effective_project_id = job.project_id or request.project_id
            # Reuse the durable scope for every downstream adapter call.  This
            # also covers direct/persisted callers whose request object omits
            # actor/session/project fields; Enterprise never falls back to a
            # worker's inherited TurnContext for those identities.
            request = replace(
                request,
                actor_user_id=(
                    str(effective_user_id).strip() if effective_user_id else None
                ),
                session_id=(
                    str(effective_session_id).strip() if effective_session_id else None
                ),
                project_id=(
                    str(effective_project_id).strip() if effective_project_id else None
                ),
            )
            await self._validate_scope(
                request,
                effective_user_id=str(effective_user_id or ""),
                effective_session_id=(
                    str(effective_session_id).strip()
                    if effective_session_id
                    else None
                ),
            )
            # Persist only server-resolved identity/scope.  In protected mode
            # an invented ``deep-research:<job>`` key is forbidden: aliases and
            # permission approvals must not outlive the authenticated session.
            job.actor_user_id = str(effective_user_id or "") or None
            job.session_id = (
                str(effective_session_id).strip() if effective_session_id else None
            )
            job.project_id = (
                str(effective_project_id).strip() if effective_project_id else None
            )
            job.metadata.update(
                {
                    "actor_user_id": job.actor_user_id,
                    "session_id": job.session_id,
                    "project_id": job.project_id,
                }
            )
            # Always bind a worker-local permission key so a ContextVar copied
            # from the request task cannot leak a previous conversation into a
            # legacy no-session Personal job.  ``default`` is a neutral scope,
            # not a synthetic job identity; protected/Enterprise jobs have
            # already been rejected above when no real session exists.
            permission_scope_token = set_permission_session_key(
                f"{effective_user_id}|{effective_session_id}"
                if effective_session_id
                else None
            )
            turn_scope_token = set_turn_context(
                user_id=str(effective_user_id or "") or None,
                project_id=request.project_id,
                session_id=(
                    str(effective_session_id).strip()
                    if effective_session_id
                    else None
                ),
            )
            self.store.save(job)
            llm = self.llm_factory(job.user_id)
            # The default adapter records direct SDK usage itself.  Preserve
            # the request scope without requiring custom test/caller factories
            # to change their one-argument contract.
            set_usage_context = getattr(llm, "set_usage_context", None)
            if callable(set_usage_context):
                try:
                    try:
                        set_usage_context(
                            user_id=job.user_id,
                            session_id=effective_session_id,
                            project_id=request.project_id,
                            session_context=request.session_context,
                            project_metadata=request.project_metadata,
                            agent_name="deep_research",
                            request_type="deep_research",
                        )
                    except TypeError:
                        # Preserve the one-argument/custom adapter contract
                        # used by existing embedding callers.
                        set_usage_context(
                            user_id=job.user_id,
                            project_id=request.project_id,
                            agent_name="deep_research",
                            request_type="deep_research",
                        )
                except Exception:
                    logger.debug("Deep research usage context setup failed", exc_info=True)
            bind_search_context = getattr(self.search_client, "bind_context", None)
            if callable(bind_search_context):
                try:
                    bind_search_context(
                        user_id=effective_user_id,
                        session_id=effective_session_id,
                        project_id=request.project_id,
                        session_context=request.session_context,
                        project_metadata=request.project_metadata,
                    )
                except Exception:
                    logger.debug("Deep research search context setup failed", exc_info=True)
            all_sources: list[DeepResearchSource] = []
            seen: set[str] = set()

            for iteration in range(1, request.max_iterations + 1):
                # Re-check the durable ACL immediately before every
                # external-capable planning phase.  A queued job must not run
                # after its conversation membership/project permission was
                # revoked.
                await self._validate_scope(
                    request,
                    effective_user_id=str(effective_user_id or ""),
                    effective_session_id=(
                        str(effective_session_id).strip()
                        if effective_session_id
                        else None
                    ),
                )
                progress_base = 8 + int((iteration - 1) * (64 / request.max_iterations))
                job.emit(
                    f"{iteration}回目の検索クエリを組み立てています",
                    progress_base,
                    "planning",
                    {"iteration": iteration},
                )
                questions = await self._await_stage(
                    self._generate_questions(
                        llm=llm,
                        query=request.query,
                        iteration=iteration,
                        request=request,
                        sources=all_sources,
                        previous_questions=job.questions_by_iteration,
                    ),
                    "planning",
                )
                job.questions_by_iteration[str(iteration)] = questions
                self.store.save(job)

                job.emit(
                    f"{len(questions)}件のクエリでソースを検索しています",
                    min(progress_base + 8, 80),
                    "search",
                    {"iteration": iteration, "questions": questions},
                )

                await self._validate_scope(
                    request,
                    effective_user_id=str(effective_user_id or ""),
                    effective_session_id=(
                        str(effective_session_id).strip()
                        if effective_session_id
                        else None
                    ),
                )

                def _search_question(question: str):
                    """Call custom/legacy search clients without losing scope."""

                    common_kwargs = {
                        "engines": request.engines,
                        "max_results_per_engine": request.max_results_per_query,
                        "project_id": effective_project_id,
                        "include_local_knowledge": request.include_local_knowledge,
                        "actor_user_id": effective_user_id,
                        "is_admin": request.is_admin,
                    }
                    scoped_kwargs = {
                        **common_kwargs,
                        "user_id": effective_user_id,
                        "session_id": effective_session_id,
                        "session_context": request.session_context,
                        "project_metadata": request.project_metadata,
                    }
                    try:
                        return self.search_client.search(question, **scoped_kwargs)
                    except TypeError as exc:
                        # Embedders may provide the pre-privacy search client
                        # contract.  Fall back only for an unsupported scope
                        # keyword; never swallow an internal TypeError.
                        message = str(exc)
                        if not any(
                            f"unexpected keyword argument '{name}'" in message
                            for name in (
                                "user_id",
                                "session_id",
                                "session_context",
                                "project_metadata",
                            )
                        ):
                            raise
                        return self.search_client.search(question, **common_kwargs)

                batches = await self._await_stage(
                    asyncio.gather(
                        *[_search_question(question) for question in questions],
                        return_exceptions=True,
                    ),
                    "engine",
                )

                added = 0
                successful_batches = 0
                transport_failures: list[BaseException] = []
                provider_failures: list[BaseException] = []
                for batch in batches:
                    if isinstance(batch, BaseException):
                        if isinstance(batch, asyncio.CancelledError):
                            raise batch
                        if isinstance(batch, (ExternalProviderBlocked, PrivacyError)):
                            raise batch
                        if isinstance(batch, DeepResearchTransportError):
                            transport_failures.append(batch)
                        elif _is_transport_failure(batch):
                            transport_failures.append(batch)
                        else:
                            provider_failures.append(batch)
                        logger.debug("Deep research query failed: %s", batch)
                        continue
                    successful_batches += 1
                    for source in batch:
                        key = _source_key(source)
                        if not key or key in seen:
                            continue
                        seen.add(key)
                        source.id = len(all_sources) + 1
                        all_sources.append(source)
                        added += 1

                # Distinguish an engine that validly returned no records from
                # a set of engines that all failed before producing a result.
                # Enterprise must not turn the latter into a successful empty
                # report (or silently fall through to another provider).
                if self._is_enterprise() and successful_batches == 0:
                    if transport_failures:
                        if any(
                            isinstance(error, (asyncio.TimeoutError, httpx.TimeoutException))
                            or getattr(error, "code", None) == "engine_timeout"
                            for error in transport_failures
                        ):
                            raise DeepResearchTransportError("engine_timeout")
                        raise DeepResearchTransportError("egress_unreachable")
                    if provider_failures:
                        raise DeepResearchProviderError()

                job.sources = all_sources
                job.emit(
                    f"{added}件の新しいソースを追加しました",
                    min(progress_base + 18, 84),
                    "search",
                    {"iteration": iteration, "total_sources": len(all_sources)},
                )
                self.store.save(job)

            await self._validate_scope(
                request,
                effective_user_id=str(effective_user_id or ""),
                effective_session_id=(
                    str(effective_session_id).strip()
                    if effective_session_id
                    else None
                ),
            )
            job.emit("収集したソースからレポートを生成しています", 88, "synthesis")
            report = await self._await_stage(
                self._synthesize_report(
                    llm, request, all_sources, job.questions_by_iteration
                ),
                "synthesis",
            )
            job.report_markdown = report
            job.status = "completed"
            job.completed_at = _utc_now()
            job.emit("調査が完了しました", 100, "completed", {"sources": len(all_sources)})
            self.store.save(job)
            return job
        except DeepResearchScopeError as exc:
            return self._terminalize(job, exc.code)
        except DeepResearchTransportError as exc:
            return self._terminalize(job, exc.code)
        except DeepResearchCredentialError:
            return self._terminalize(job, "credential_missing")
        except DeepResearchProviderError as exc:
            return self._terminalize(job, exc.code)
        except DeepResearchStageTimeout as exc:
            return self._terminalize(job, f"{exc.stage}_timeout")
        except (ExternalProviderBlocked, PrivacyError):
            return self._terminalize(job, "privacy_protection_failed")
        except asyncio.TimeoutError:
            return self._terminalize(job, "deadline")
        except Exception:
            # Error details may contain provider URLs, credentials or internal
            # hostnames.  Persist only a stable code/message and keep details in
            # server logs for operators.
            logger.exception("Deep research job failed")
            return self._terminalize(job, "internal_error")
        finally:
            if privacy_policy_token is not None:
                reset_privacy_policy_context(privacy_policy_token)
            if turn_scope_token is not None:
                reset_turn_context(turn_scope_token)
            if permission_scope_token is not None:
                reset_permission_session_key(permission_scope_token)

    async def _generate_questions(
        self,
        *,
        llm: Any,
        query: str,
        iteration: int,
        request: DeepResearchRequest,
        sources: list[DeepResearchSource],
        previous_questions: dict[str, list[str]],
    ) -> list[str]:
        if iteration == 1:
            base = [query]
        else:
            base = []

        source_summary = "\n".join(
            f"- [{source.id}] {source.title}: {_truncate(source.snippet, 180)}"
            for source in sources[-12:]
        )
        prompt = f"""今日の日付は {datetime.now().date().isoformat()} です。
次の調査テーマについて、未確認の論点を埋める検索クエリを {request.questions_per_iteration} 件作ってください。

調査テーマ:
{query}

これまでの検索:
{json.dumps(previous_questions, ensure_ascii=False)}

現在のソース概要:
{source_summary or "まだありません"}

出力は検索クエリだけにしてください。各行を `Q: ...` の形式にしてください。"""

        try:
            response = await llm.generate(prompt, max_tokens=800)
            generated = self._parse_questions(response)
            if self._is_enterprise() and not generated:
                # A successful HTTP response with no usable planning output is
                # not a safe basis for an implicit fallback query.  Keep the
                # terminal state truthful instead of manufacturing work.
                raise DeepResearchProviderError()
        except (ExternalProviderBlocked, PrivacyError):
            # A privacy/permission failure is a hard boundary failure.  Do not
            # turn it into fallback questions that would trigger an external
            # search with an unverified payload.
            raise
        except Exception as exc:
            if self._is_enterprise():
                code = _provider_failure_code(exc, self.config)
                if code == "credential_missing":
                    raise DeepResearchCredentialError() from exc
                if code in {"engine_timeout", "egress_unreachable"}:
                    raise DeepResearchTransportError(code) from exc
                raise DeepResearchProviderError(code) from exc
            logger.warning("Question generation failed: %s", exc)
            generated = []

        questions = []
        for question in [*base, *generated]:
            clean = question.strip()
            if clean and clean not in questions:
                questions.append(clean)
            if len(questions) >= request.questions_per_iteration:
                break

        if not questions:
            questions = self._fallback_questions(query, iteration, request.questions_per_iteration)
        return questions[: request.questions_per_iteration]

    def _parse_questions(self, response: str) -> list[str]:
        questions: list[str] = []
        for line in (response or "").splitlines():
            line = line.strip()
            if not line:
                continue
            line = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line)
            if line.lower().startswith("q:"):
                line = line[2:].strip()
            if len(line) >= 4:
                questions.append(line)
        return questions

    def _fallback_questions(self, query: str, iteration: int, count: int) -> list[str]:
        suffixes = [
            "",
            " latest research evidence",
            " key sources and citations",
            " criticism limitations risks",
            " timeline recent developments",
            " academic review",
        ]
        offset = max(0, iteration - 1)
        return [f"{query}{suffixes[(offset + i) % len(suffixes)]}".strip() for i in range(count)]

    async def _synthesize_report(
        self,
        llm: Any,
        request: DeepResearchRequest,
        sources: list[DeepResearchSource],
        questions_by_iteration: dict[str, list[str]],
    ) -> str:
        if not sources:
            return (
                f"# Deep Research: {request.query}\n\n"
                "有効な引用ソースを取得できませんでした。SearXNGのURL設定、ネットワーク、検索語を確認してください。"
            )

        source_block = "\n\n".join(
            (
                f"[{source.id}] {source.title}\n"
                f"Engine: {source.engine}\n"
                f"URL: {source.url or '(local)'}\n"
                f"Published: {source.published_at or 'unknown'}\n"
                f"Snippet: {_truncate(source.snippet, 900)}"
            )
            for source in sources[:60]
        )
        depth_instruction = {
            "quick": "要点中心に短くまとめてください。",
            "detailed": "主要論点、根拠、未確定点を整理してください。",
            "report": "見出しを分けた調査レポートとして詳しくまとめてください。",
        }.get(request.mode, "主要論点、根拠、未確定点を整理してください。")
        prompt = f"""あなたはローカル実行のDeep Researchエージェントです。
下記ソースだけを根拠に、Markdownで日本語の調査レポートを書いてください。
出典番号は必ず [1] のような角括弧で本文中に入れてください。URLや出典を捏造しないでください。

調査テーマ:
{request.query}

検索計画:
{json.dumps(questions_by_iteration, ensure_ascii=False, indent=2)}

ソース:
{source_block}

要件:
- {depth_instruction}
- 最初に結論を置く
- 根拠が弱い点は「未確認」または「追加調査が必要」と明記する
- 最後に「次に調べるべきこと」を3項目以内で出す
"""
        token = set_verified_tool_execution_claims(
            [
                self._deep_research_search_record(
                    request,
                    sources,
                    questions_by_iteration,
                )
            ]
        )
        try:
            report = await llm.generate(prompt, max_tokens=4096)
            if self._is_enterprise() and not str(report or "").strip():
                raise DeepResearchProviderError()
        except (ExternalProviderBlocked, PrivacyError):
            raise
        except Exception as exc:
            if self._is_enterprise():
                code = _provider_failure_code(exc, self.config)
                if code == "credential_missing":
                    raise DeepResearchCredentialError() from exc
                if code in {"engine_timeout", "egress_unreachable"}:
                    raise DeepResearchTransportError(code) from exc
                raise DeepResearchProviderError(code) from exc
            logger.warning("Report synthesis failed: %s", exc)
            report = self._fallback_report(request.query, sources)
        finally:
            reset_verified_tool_execution_claims(token)

        return self._append_bibliography(report.strip(), sources)

    def _deep_research_search_record(
        self,
        request: DeepResearchRequest,
        sources: list[DeepResearchSource],
        questions_by_iteration: dict[str, list[str]],
    ) -> OpenAIToolCallRecord:
        source_lines = []
        for source in sources[:20]:
            source_lines.append(
                (
                    f"[{source.id}] {source.title} "
                    f"({source.engine}) {source.url or '(local)'}"
                ).strip()
            )

        questions = [
            question
            for items in questions_by_iteration.values()
            for question in items
        ]
        result = "\n".join(
            [
                "Deep Research search completed successfully.",
                f"Topic: {request.query}",
                f"Search queries: {len(questions)}",
                f"Sources collected: {len(sources)}",
                *source_lines,
            ]
        )
        return OpenAIToolCallRecord(
            tool="web_search",
            arguments={
                "request": request.query,
                "source": "deep_research",
            },
            result=result,
        )

    def _fallback_report(self, query: str, sources: list[DeepResearchSource]) -> str:
        lines = [f"# Deep Research: {query}", "", "## 収集ソースの要約"]
        for source in sources[:10]:
            lines.append(f"- [{source.id}] {source.title}: {_truncate(source.snippet, 220)}")
        return "\n".join(lines)

    def _append_bibliography(self, report: str, sources: list[DeepResearchSource]) -> str:
        bibliography = ["", "## 参考ソース"]
        for source in sources:
            label = source.url or source.engine
            bibliography.append(f"{source.id}. {source.title} - {label}")
        if "## 参考ソース" in report:
            return report
        return f"{report}\n{chr(10).join(bibliography)}"


class DeepResearchManager:
    def __init__(
        self,
        *,
        config: Any,
        store: Optional[DeepResearchJobStore] = None,
        runner: Optional[DeepResearchRunner] = None,
    ) -> None:
        self.config = config
        self.store = store or DeepResearchJobStore()
        self.runner = runner or DeepResearchRunner(config=config, store=self.store)
        self._tasks: dict[str, asyncio.Task] = {}
        self._detached_tasks: dict[str, asyncio.Task] = {}
        self._active_jobs: dict[str, DeepResearchJob] = {}
        self._job_workers: dict[str, asyncio.Task] = {}
        self._worker_cancel_requested: set[asyncio.Task] = set()
        self._shutdown_requested: set[str] = set()
        self._workers: list[asyncio.Task] = []
        self._queue: asyncio.Queue[tuple[DeepResearchJob, DeepResearchRequest]] | None = None
        self._closed = False
        self._queue_capacity = self._bounded_int(
            _config_get(config, "deep_research.queue_capacity", DEFAULT_QUEUE_CAPACITY),
            minimum=1,
            maximum=256,
            default=DEFAULT_QUEUE_CAPACITY,
        )
        self._worker_count = self._bounded_int(
            _config_get(config, "deep_research.worker_count", DEFAULT_WORKER_COUNT),
            minimum=1,
            maximum=32,
            default=DEFAULT_WORKER_COUNT,
        )
        self._shutdown_timeout_seconds = self._bounded_float(
            _config_get(
                config,
                "deep_research.shutdown_timeout_seconds",
                DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
            ),
            minimum=1.0,
            maximum=30.0,
            default=DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
        )
        # A new process must not claim work left by a previous process without
        # an external lease.  Terminalize stale records before exposing list/
        # get APIs or accepting new jobs.
        reconcile = getattr(self.store, "reconcile_stale_jobs", None)
        if callable(reconcile):
            reconcile()

    def reopen(self) -> None:
        """Re-arm a manager for a subsequent application lifespan.

        FastAPI/WebChatServer test and development lifespans can be entered
        more than once in one Python process.  Shutdown intentionally closes
        the current queue and workers, but that terminal state must not leak
        into the next startup.  Reopening is only valid after the previous
        shutdown has drained all owned tasks; a live manager is left intact.
        """

        if self._tasks or self._workers:
            return
        self._closed = False
        # Detached provider tasks are allowed to finish in the background
        # after a bounded shutdown.  Keep their cancellation markers until
        # their callbacks consume them, but do not let them prevent a new
        # lifespan from accepting fresh work.
        self._shutdown_requested.intersection_update(self._detached_tasks)
        self._queue = None
        reconcile = getattr(self.store, "reconcile_stale_jobs", None)
        if callable(reconcile):
            reconcile()

    @staticmethod
    def _bounded_int(value: Any, *, minimum: int, maximum: int, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(parsed, maximum))

    @staticmethod
    def _bounded_float(value: Any, *, minimum: float, maximum: float, default: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = default
        if not math.isfinite(parsed):
            parsed = default
        return max(minimum, min(parsed, maximum))

    def _ensure_workers(self) -> None:
        if self._closed:
            raise DeepResearchManagerClosedError()
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self._queue_capacity)
        self._workers = [worker for worker in self._workers if not worker.done()]
        while len(self._workers) < self._worker_count:
            # A request handler may carry a TurnContext/permission key.  Do
            # not inherit it into a process-wide worker; the runner binds the
            # persisted job scope explicitly for the duration of its task.
            self._workers.append(self._create_neutral_task(self._worker_loop()))

    @staticmethod
    def _create_neutral_task(coro: Awaitable[Any]) -> asyncio.Task:
        """Create a task with a fresh, empty ContextVar context.

        ``context=`` is available on newer Python versions.  The fallback
        schedules through ``Context.run`` for the Python 3.10 runtime still
        supported by some AoiTalk deployments.
        """

        context = contextvars.Context()
        try:
            return asyncio.create_task(coro, context=context)
        except TypeError:  # pragma: no cover - exercised on Python 3.10
            return context.run(asyncio.create_task, coro)

    async def _worker_loop(self) -> None:
        if self._queue is None:  # pragma: no cover - ensured by _ensure_workers
            return
        queue = self._queue
        while True:
            job, request = await queue.get()
            try:
                stored = self.store.load(job.id)
                if stored is not None:
                    job = stored
                if job.status in TERMINAL_STATUSES:
                    continue
                task = self._create_neutral_task(self.runner.run(job, request))
                self._tasks[job.id] = task
                self._active_jobs[job.id] = job
                worker_task = asyncio.current_task()
                if worker_task is not None:
                    self._job_workers[job.id] = worker_task
                try:
                    # Do not allow cancellation of the worker itself to make
                    # us synchronously wait for a provider task that suppresses
                    # cancellation. The manager owns the child and can detach
                    # it after the bounded shutdown window.
                    result = await asyncio.shield(task)
                    if job.id in self._shutdown_requested:
                        # A stubborn child may ignore cancellation and return
                        # a completed object after shutdown has already
                        # settled the job as cancelled.  Never let that late
                        # result resurrect a terminal cancellation.
                        latest = self.store.load(job.id) or job
                        self._force_cancelled(latest)
                        continue
                    # A custom runner is allowed to return a job object, but
                    # it must never leave a non-terminal persisted record.
                    settled = result if isinstance(result, DeepResearchJob) else job
                    if settled.status not in TERMINAL_STATUSES:
                        settled = self.runner._terminalize(settled, "internal_error")
                        self.store.save(settled)
                except asyncio.CancelledError:
                    current_worker = asyncio.current_task()
                    worker_cancelled = (
                        self._closed
                        or (
                            current_worker is not None
                            and current_worker in self._worker_cancel_requested
                        )
                        or bool(
                            current_worker is not None
                            and getattr(current_worker, "cancelling", lambda: 0)()
                        )
                    )
                    if current_worker is not None:
                        self._worker_cancel_requested.discard(current_worker)
                    if not task.done():
                        task.cancel()
                    latest = self.store.load(job.id) or job
                    if job.id in self._shutdown_requested:
                        self._force_cancelled(latest)
                    else:
                        self._cancel_if_nonterminal(latest)
                    if not task.done():
                        self._detach_task(job.id, task)
                    if worker_cancelled:
                        raise
                    # Cancellation of the child task itself (for example a
                    # user-requested cancel) must not kill the shared worker
                    # lane.  Continue to the next queued job after settling
                    # this record.
                    continue
                except Exception:
                    logger.exception("Deep research worker task failed")
                    latest = self.store.load(job.id) or job
                    if latest.status not in TERMINAL_STATUSES:
                        latest = self.runner._terminalize(latest, "internal_error")
                        self.store.save(latest)
                finally:
                    self._tasks.pop(job.id, None)
                    self._active_jobs.pop(job.id, None)
                    self._job_workers.pop(job.id, None)
                    if job.id not in self._detached_tasks:
                        self._shutdown_requested.discard(job.id)
            finally:
                queue.task_done()

    async def shutdown(self) -> None:
        """Cancel and await workers/active jobs during application shutdown."""

        if (
            self._closed
            and not self._workers
            and not self._tasks
            and not self._detached_tasks
        ):
            return
        self._closed = True

        # Anything still in the queue has not acquired a runner task.  Drain
        # and terminalize it so shutdown cannot strand records in ``queued``.
        queue = self._queue
        if queue is not None:
            while True:
                try:
                    queued_job, _request = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                try:
                    latest = self.store.load(queued_job.id) or queued_job
                    if latest.status not in TERMINAL_STATUSES:
                        latest = self.runner._terminalize(
                            latest, "cancelled", status="cancelled"
                        )
                        self.store.save(latest)
                finally:
                    queue.task_done()

        workers = list(self._workers)
        active_items = list(self._tasks.items())
        for job_id, task in active_items:
            if not task.done():
                # Write the cancellation tombstone before delivering
                # cancellation to the child. This closes the event-loop
                # window in which a cancellation-suppressing provider could
                # save a late result.
                latest = self.store.load(job_id) or self._active_jobs.get(job_id)
                if latest is not None:
                    self._force_cancelled(latest)
                self._shutdown_requested.add(job_id)
                task.cancel()
            else:
                # A task that already reached a terminal state before shutdown
                # keeps its result (notably a successful completion).
                latest = self.store.load(job_id)
                if latest is not None:
                    self._cancel_if_nonterminal(latest)
        for worker in workers:
            self._worker_cancel_requested.add(worker)
            worker.cancel()
        # Do not let a misbehaving provider that suppresses cancellation hold
        # application shutdown forever.  ``asyncio.wait`` returns after the
        # bounded drain window even when a child ignores its cancellation;
        # those jobs are persisted as cancelled and their late exceptions are
        # consumed by a callback.
        if active_items:
            active_tasks = [task for _job_id, task in active_items]
            _done, pending = await asyncio.wait(
                active_tasks,
                timeout=self._shutdown_timeout_seconds,
            )
            for job_id, task in active_items:
                if task not in pending:
                    try:
                        task.exception()
                    except BaseException:
                        pass
                    latest = self.store.load(job_id)
                    if job_id in self._shutdown_requested and latest is not None:
                        self._force_cancelled(latest)
                    continue
                latest = self.store.load(job_id)
                if latest is not None:
                    self._force_cancelled(latest)
                self._detach_task(job_id, task)
        if workers:
            _done, pending_workers = await asyncio.wait(
                workers,
                timeout=self._shutdown_timeout_seconds,
            )
            for worker in pending_workers:
                worker.add_done_callback(self._consume_task_exception)
        self._tasks.clear()
        self._workers.clear()
        self._active_jobs.clear()
        self._job_workers.clear()
        self._worker_cancel_requested.clear()

    @staticmethod
    def _consume_task_exception(task: asyncio.Task) -> None:
        try:
            task.exception()
        except BaseException:
            pass

    def _settle_late_task(self, job_id: str, task: asyncio.Task) -> None:
        """Consume a detached child and preserve its shutdown terminal state."""

        try:
            task.exception()
        except BaseException:
            pass
        if self._detached_tasks.get(job_id) is task:
            self._detached_tasks.pop(job_id, None)
        if job_id not in self._shutdown_requested:
            return
        latest = self.store.load(job_id)
        if latest is None:
            self._shutdown_requested.discard(job_id)
            return
        self._force_cancelled(latest)
        self._shutdown_requested.discard(job_id)

    def _detach_task(self, job_id: str, task: asyncio.Task) -> None:
        """Track a child that outlived its bounded manager drain window."""

        self._shutdown_requested.add(job_id)
        if self._detached_tasks.get(job_id) is task:
            return
        self._detached_tasks[job_id] = task
        task.add_done_callback(
            lambda finished, detached_job_id=job_id: self._settle_late_task(
                detached_job_id, finished
            )
        )

    def _cancel_if_nonterminal(self, job: DeepResearchJob) -> None:
        """Persist cancellation without overwriting an already terminal result."""

        if job.status in TERMINAL_STATUSES:
            return
        job = self.runner._terminalize(job, "cancelled", status="cancelled")
        self.store.save(job)

    def _force_cancelled(self, job: DeepResearchJob) -> None:
        """Persist cancellation without overwriting a truthful terminal result.

        ``DeepResearchJobStore.save`` rejects stale status transitions from a
        cancellation tombstone, so a detached provider cannot resurrect a
        report after shutdown.  If a provider had already reached ``completed``
        or ``failed`` before shutdown, preserve that terminal result instead of
        rewriting its error/report as cancellation.
        """

        self._cancel_if_nonterminal(job)

    close = shutdown

    def available_engines(self) -> list[dict[str, Any]]:
        return self.runner.search_client.available_engines()

    def list_jobs(self, *, user_id: Optional[str], limit: int = 30) -> list[DeepResearchJob]:
        return self.store.list_jobs(limit=limit, user_id=user_id)

    def get_job(self, job_id: str, *, user_id: Optional[str] = None) -> Optional[DeepResearchJob]:
        job = self.store.load(job_id)
        if not job:
            return None
        if user_id and job.user_id != user_id:
            return None
        return job

    async def start_job(self, request: DeepResearchRequest, *, user_id: str) -> DeepResearchJob:
        if self._closed:
            raise DeepResearchManagerClosedError()
        normalized = request.normalized(
            enterprise_public_egress_approved=approved_public_egress(self.config)
        )
        self._ensure_workers()
        assert self._queue is not None
        # Check before persisting so a rejected request never leaves a ghost
        # job record.  ``put_nowait`` cannot be interleaved by another task in
        # this synchronous section of the event loop.
        if self._queue.full():
            raise DeepResearchQueueFullError()
        job = DeepResearchJob(
            id=str(uuid.uuid4()),
            user_id=user_id,
            query=normalized.query,
            mode=normalized.mode,
            metadata={
                "engines": normalized.engines,
                "max_iterations": normalized.max_iterations,
                "questions_per_iteration": normalized.questions_per_iteration,
                "include_local_knowledge": normalized.include_local_knowledge,
                "project_id": normalized.project_id,
                "actor_user_id": normalized.actor_user_id,
                "is_admin": normalized.is_admin,
                "session_id": normalized.session_id,
                "session_context": dict(normalized.session_context or {}),
                "project_metadata": dict(normalized.project_metadata or {}),
                "privacy_snapshot": _privacy_snapshot(
                    normalized.session_context,
                    normalized.project_metadata,
                    project_id=normalized.project_id,
                    global_policy=_config_get(
                        self.config, "external_model_privacy", {}
                    ),
                ),
            },
            actor_user_id=normalized.actor_user_id,
            session_id=normalized.session_id,
            project_id=normalized.project_id,
            privacy_snapshot=_privacy_snapshot(
                normalized.session_context,
                normalized.project_metadata,
                project_id=normalized.project_id,
                global_policy=_config_get(
                    self.config, "external_model_privacy", {}
                ),
            ),
        )
        job.emit("キューに追加しました", 0, "queued")
        self.store.save(job)
        try:
            self._queue.put_nowait((job, normalized))
        except asyncio.QueueFull as exc:
            # Keep persistence and queue admission atomic from the caller's
            # point of view: remove the queued record's ghost terminally and
            # surface a retryable queue error instead of leaving ``queued``.
            job = self.runner._terminalize(job, "queue_full")
            self.store.save(job)
            raise DeepResearchQueueFullError() from exc
        return job

    async def cancel_job(
        self,
        job_id: str,
        *,
        user_id: Optional[str] = None,
    ) -> Optional[DeepResearchJob]:
        job = self.get_job(job_id, user_id=user_id)
        if job is None:
            return None
        task = self._tasks.get(job.id)
        if task is not None and not task.done():
            task.cancel()
            _done, pending = await asyncio.wait(
                {task}, timeout=self._shutdown_timeout_seconds
            )
            if pending:
                self._detach_task(job.id, task)
                worker = self._job_workers.get(job.id)
                current = asyncio.current_task()
                if worker is not None and worker is not current and not worker.done():
                    # A cancelled stubborn child must not pin the worker lane
                    # forever.  The worker's cancellation handler detaches the
                    # child and ``_ensure_workers`` will replace the worker on
                    # the next admission.
                    self._worker_cancel_requested.add(worker)
                    worker.cancel()
                    # Retire the canceled worker from capacity immediately and
                    # replace it now.  Waiting for a future ``start_job`` call
                    # would leave jobs already queued behind a stubborn child
                    # indefinitely.
                    self._workers = [
                        item
                        for item in self._workers
                        if item is not worker and not item.done()
                    ]
                    worker.add_done_callback(self._consume_task_exception)
                    if not self._closed:
                        self._ensure_workers()
                    await asyncio.wait({worker}, timeout=0.1)
            # ``runner.run`` normally settles cancellation itself, but custom
            # runners (and a cancellation racing startup) may propagate the
            # CancelledError before persisting.  Re-read and settle here so a
            # caller never observes a permanently queued/running job.
        # Always re-read after a cancellation/completion race.  The initial
        # `get_job` snapshot may still say ``running`` while the worker has
        # already persisted a completed report; never overwrite that terminal
        # result with a late cancellation.
        latest = self.store.load(job.id) or job
        if task is not None and task.done():
            try:
                task_result = task.result()
            except asyncio.CancelledError:
                task_result = None
            except Exception:
                task_result = None
            if isinstance(task_result, DeepResearchJob) and task_result.status in TERMINAL_STATUSES:
                latest = self.store.load(job.id) or task_result
                if latest.status not in TERMINAL_STATUSES:
                    self.store.save(task_result)
                    latest = task_result
        if latest.status not in TERMINAL_STATUSES:
            latest = self.runner._terminalize(
                latest, "cancelled", status="cancelled"
            )
            self.store.save(latest)
        return self.store.load(job.id) or latest
