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
import html
import json
import logging
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
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
    ExternalProviderBlocked,
    OutboundPrivacyGateway,
    PrivacyError,
)
from .turn_context import get_turn_context

logger = logging.getLogger(__name__)


DEFAULT_ENGINES = ["searxng", "wikipedia", "arxiv", "openalex", "pubmed"]
DEFAULT_YAHOO_REALTIME_URL = "https://search.yahoo.co.jp/realtime/search"
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    if hasattr(config, "get"):
        try:
            value = config.get(key, default)
            if value is not None:
                return value
        except Exception:
            pass
    if isinstance(config, dict):
        value: Any = config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value
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

    def normalized(self) -> "DeepResearchRequest":
        mode = self.mode if self.mode in {"quick", "detailed", "report"} else "detailed"
        default_iterations = {"quick": 1, "detailed": 3, "report": 4}[mode]
        max_iterations = self.max_iterations or default_iterations
        return DeepResearchRequest(
            query=self.query.strip(),
            mode=mode,
            max_iterations=max(1, min(int(max_iterations), 8)),
            questions_per_iteration=max(1, min(int(self.questions_per_iteration), 6)),
            max_results_per_query=max(1, min(int(self.max_results_per_query), 10)),
            engines=[e for e in (self.engines or DEFAULT_ENGINES) if e],
            include_local_knowledge=bool(self.include_local_knowledge),
            project_id=self.project_id,
            actor_user_id=self.actor_user_id,
            is_admin=bool(self.is_admin),
            session_id=str(self.session_id).strip() if self.session_id else None,
            session_context=(
                dict(self.session_context)
                if isinstance(self.session_context, Mapping)
                else None
            ),
            project_metadata=(
                dict(self.project_metadata)
                if isinstance(self.project_metadata, Mapping)
                else None
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
            events=events,
            questions_by_iteration=data.get("questions_by_iteration", {}),
            sources=sources,
            report_markdown=data.get("report_markdown", ""),
            metadata=data.get("metadata", {}),
        )


class DeepResearchJobStore:
    """Small JSON-backed store for restart-tolerant research history."""

    def __init__(self, base_dir: Path | str = "cache/deep_research") -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save(self, job: DeepResearchJob) -> None:
        path = self.base_dir / f"{_safe_filename(job.id)}.json"
        path.write_text(
            json.dumps(job.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

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
        protected = await self._privacy_gateway.protect(
            {"prompt": prompt},
            provider="gemini",
            source_kind="deep_research_model_request",
        )
        outbound_prompt = str((protected.payload or {}).get("prompt") or "")
        if hasattr(model, "generate_content_async"):
            response = await model.generate_content_async(outbound_prompt)
        else:
            response = await asyncio.to_thread(model.generate_content, outbound_prompt)
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
        protected = await self._privacy_gateway.protect(
            request_kwargs,
            provider="openai",
            base_url=str(getattr(client, "base_url", "") or ""),
            source_kind="deep_research_model_request",
        )
        response = await client.responses.create(
            **protected.payload
        )
        self._record_direct_usage(
            response,
            provider="openai",
            model=model_name,
            started=started,
        )
        return self._privacy_gateway.restore(getattr(response, "output_text", "") or "")

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
        protected = await self._privacy_gateway.protect(
            request_kwargs,
            provider=str(_config_get(self.config, "llm_provider", "openai_compatible_local")),
            base_url=base_url,
            source_kind="deep_research_model_request",
        )
        response = await client.chat.completions.create(
            **protected.payload
        )
        self._record_direct_usage(
            response,
            provider=str(_config_get(self.config, "llm_provider", "openai_compatible_local")),
            model=str(model),
            started=started,
        )
        return self._privacy_gateway.restore(response.choices[0].message.content or "")

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
        protected = await self._privacy_gateway.protect(
            {"prompt": prompt},
            provider=provider,
            base_url=base_url,
            source_kind="deep_research_model_request_fallback",
        )
        outbound_prompt = str((protected.payload or {}).get("prompt") or "")
        if hasattr(client, "generate_response_async"):
            result = await client.generate_response_async(outbound_prompt)
        else:
            result = await asyncio.to_thread(client.generate_response, outbound_prompt)
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

    async def _protect_payload(
        self,
        payload: Mapping[str, Any],
        *,
        provider: str,
        base_url: str,
        source_kind: str,
    ) -> dict[str, Any]:
        """Protect one request immediately before its network transport."""

        protected = await self._privacy_gateway.protect(
            dict(payload),
            provider=provider,
            base_url=base_url,
            source_kind=source_kind,
        )
        if isinstance(protected.payload, Mapping):
            return dict(protected.payload)
        return dict(payload)

    async def _protected_query(
        self,
        query: str,
        *,
        provider: str,
        base_url: str,
        source_kind: str = "deep_research_search",
    ) -> str:
        payload = await self._protect_payload(
            {"query": str(query or "")},
            provider=provider,
            base_url=base_url,
            source_kind=source_kind,
        )
        return str(payload.get("query") or query or "")

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
        tasks: list[Awaitable[list[DeepResearchSource]]] = []
        selected = [engine.lower() for engine in engines]
        # Fail closed before opening an AsyncClient or scheduling any external
        # search transport.  In particular, local_only must not degrade to an
        # empty result set that looks like a successful search.
        searxng_url = self._searxng_url()
        if "searxng" in selected and searxng_url:
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
        yahoo_url = self._yahoo_realtime_url()
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
            self._privacy_gateway.ensure_provider_allowed(
                "openai",
                base_url=self._external_base_url(engine),
            )
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
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
                except (ExternalProviderBlocked, PrivacyError):
                    raise
                except Exception as exc:  # provider failure must not hide others
                    logger.debug("Yahoo realtime search failed: %s", exc)
            if "searxng" in selected:
                if self._searxng_url():
                    tasks.append(self._search_searxng(client, query, max_results_per_engine))
                elif "duckduckgo" not in selected:
                    tasks.append(self._search_duckduckgo(client, query, max_results_per_engine))
            if "duckduckgo" in selected:
                tasks.append(self._search_duckduckgo(client, query, max_results_per_engine))
            if "yahoo_realtime" in selected and not (x_intent and yahoo_url):
                if yahoo_url:
                    tasks.append(
                        self._search_yahoo_realtime(
                            client, query, max_results_per_engine
                        )
                    )
            if "wikipedia" in selected:
                tasks.append(self._search_wikipedia(client, query, max_results_per_engine))
            if "arxiv" in selected:
                tasks.append(self._search_arxiv(client, query, max_results_per_engine))
            if "openalex" in selected:
                tasks.append(self._search_openalex(client, query, max_results_per_engine))
            if "pubmed" in selected:
                tasks.append(self._search_pubmed(client, query, max_results_per_engine))
            if include_local_knowledge:
                tasks.append(
                    self._search_local_knowledge(
                        query,
                        max_results_per_engine,
                        project_id,
                        actor_user_id=actor_user_id,
                        is_admin=is_admin,
                    )
                )

            batches.extend(await asyncio.gather(*tasks, return_exceptions=True))

        results: list[DeepResearchSource] = []
        for batch in batches:
            if isinstance(batch, Exception):
                if isinstance(batch, (ExternalProviderBlocked, PrivacyError)):
                    raise batch
                logger.debug("Deep research search batch failed: %s", batch)
                continue
            results.extend(batch)
        return self._dedupe(results)

    def available_engines(self) -> list[dict[str, Any]]:
        searxng_url = self._searxng_url()
        return [
            {"id": "searxng", "label": "SearXNG", "available": bool(searxng_url)},
            {
                "id": "yahoo_realtime",
                "label": "Yahoo!リアルタイム検索",
                "available": bool(self._yahoo_realtime_url()),
            },
            {"id": "duckduckgo", "label": "DuckDuckGo HTML", "available": True},
            {"id": "wikipedia", "label": "Wikipedia", "available": True},
            {"id": "arxiv", "label": "arXiv", "available": True},
            {"id": "openalex", "label": "OpenAlex", "available": True},
            {"id": "pubmed", "label": "PubMed", "available": True},
        ]

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
        outbound_query = await self._protected_query(
            query,
            provider="openai",
            base_url=self._external_base_url("duckduckgo"),
            source_kind="deep_research_search_duckduckgo",
        )
        response = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": outbound_query, "kl": "jp-jp"},
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
        outbound_query = await self._protected_query(
            query,
            provider="openai_compatible_local",
            base_url=base_url,
            source_kind="deep_research_search_searxng",
        )
        response = await client.get(
            f"{base_url}/search",
            params={
                "q": outbound_query,
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
            outbound_query = await self._protected_query(
                query,
                provider="openai",
                base_url=f"https://{lang}.wikipedia.org/w/api.php",
                source_kind="deep_research_search_wikipedia",
            )
            response = await client.get(
                f"https://{lang}.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": outbound_query,
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

        batches = await asyncio.gather(run_language("ja"), run_language("en"), return_exceptions=True)
        sources: list[DeepResearchSource] = []
        for batch in batches:
            if isinstance(batch, Exception):
                if isinstance(batch, (ExternalProviderBlocked, PrivacyError)):
                    raise batch
                continue
            sources.extend(batch)
        return sources[:limit]

    async def _search_arxiv(
        self, client: httpx.AsyncClient, query: str, limit: int
    ) -> list[DeepResearchSource]:
        outbound_query = await self._protected_query(
            query,
            provider="openai",
            base_url=self._external_base_url("arxiv"),
            source_kind="deep_research_search_arxiv",
        )
        params = urlencode(
            {"search_query": f"all:{outbound_query}", "start": 0, "max_results": limit}
        )
        response = await client.get(f"https://export.arxiv.org/api/query?{params}")
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
        outbound_query = await self._protected_query(
            query,
            provider="openai",
            base_url=self._external_base_url("openalex"),
            source_kind="deep_research_search_openalex",
        )
        response = await client.get(
            "https://api.openalex.org/works",
            params={
                "search": outbound_query,
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
        outbound_query = await self._protected_query(
            query,
            provider="openai",
            base_url=self._external_base_url("pubmed"),
            source_kind="deep_research_search_pubmed",
        )
        search = await client.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            params={
                "db": "pubmed",
                "term": outbound_query,
                "retmode": "json",
                "retmax": limit,
            },
            headers={"User-Agent": "AoiTalkDeepResearch/0.1"},
        )
        search.raise_for_status()
        ids = search.json().get("esearchresult", {}).get("idlist", [])
        if not ids:
            return []
        summary_payload = await self._protect_payload(
            {
                "db": "pubmed",
                "id": ",".join(str(item) for item in ids),
                "retmode": "json",
            },
            provider="openai",
            base_url=self._external_base_url("pubmed"),
            source_kind="deep_research_search_pubmed_summary",
        )
        summary = await client.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
            params={
                "db": str(summary_payload.get("db") or "pubmed"),
                "id": str(summary_payload.get("id") or ",".join(ids)),
                "retmode": str(summary_payload.get("retmode") or "json"),
            },
            headers={"User-Agent": "AoiTalkDeepResearch/0.1"},
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

    async def run(self, job: DeepResearchJob, request: DeepResearchRequest) -> DeepResearchJob:
        request = request.normalized()
        job.status = "running"
        job.started_at = _utc_now()
        job.updated_at = job.started_at
        job.emit("調査を開始しました", 2, "setup", {"mode": request.mode})
        self.store.save(job)

        try:
            llm = self.llm_factory(job.user_id)
            turn = get_turn_context()
            effective_session_id = (
                request.session_id
                or turn.session_id
                or f"deep-research:{job.id}"
            )
            effective_user_id = request.actor_user_id or job.user_id
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
                progress_base = 8 + int((iteration - 1) * (64 / request.max_iterations))
                job.emit(
                    f"{iteration}回目の検索クエリを組み立てています",
                    progress_base,
                    "planning",
                    {"iteration": iteration},
                )
                questions = await self._generate_questions(
                    llm=llm,
                    query=request.query,
                    iteration=iteration,
                    request=request,
                    sources=all_sources,
                    previous_questions=job.questions_by_iteration,
                )
                job.questions_by_iteration[str(iteration)] = questions
                self.store.save(job)

                job.emit(
                    f"{len(questions)}件のクエリでソースを検索しています",
                    min(progress_base + 8, 80),
                    "search",
                    {"iteration": iteration, "questions": questions},
                )

                def _search_question(question: str):
                    """Call custom/legacy search clients without losing scope."""

                    common_kwargs = {
                        "engines": request.engines,
                        "max_results_per_engine": request.max_results_per_query,
                        "project_id": request.project_id,
                        "include_local_knowledge": request.include_local_knowledge,
                        "actor_user_id": request.actor_user_id,
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

                batches = await asyncio.gather(
                    *[_search_question(question) for question in questions],
                    return_exceptions=True,
                )

                added = 0
                for batch in batches:
                    if isinstance(batch, Exception):
                        if isinstance(batch, (ExternalProviderBlocked, PrivacyError)):
                            raise batch
                        logger.debug("Deep research query failed: %s", batch)
                        continue
                    for source in batch:
                        key = _source_key(source)
                        if not key or key in seen:
                            continue
                        seen.add(key)
                        source.id = len(all_sources) + 1
                        all_sources.append(source)
                        added += 1

                job.sources = all_sources
                job.emit(
                    f"{added}件の新しいソースを追加しました",
                    min(progress_base + 18, 84),
                    "search",
                    {"iteration": iteration, "total_sources": len(all_sources)},
                )
                self.store.save(job)

            job.emit("収集したソースからレポートを生成しています", 88, "synthesis")
            report = await self._synthesize_report(llm, request, all_sources, job.questions_by_iteration)
            job.report_markdown = report
            job.status = "completed"
            job.completed_at = _utc_now()
            job.emit("調査が完了しました", 100, "completed", {"sources": len(all_sources)})
            self.store.save(job)
            return job
        except Exception as exc:
            logger.exception("Deep research job failed")
            job.status = "failed"
            job.error = str(exc)
            job.completed_at = _utc_now()
            job.emit("調査が失敗しました", job.progress, "failed", {"error": str(exc)})
            self.store.save(job)
            return job

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
        except Exception as exc:
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
        except Exception as exc:
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
        normalized = request.normalized()
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
            },
        )
        job.emit("キューに追加しました", 0, "queued")
        self.store.save(job)
        task = asyncio.create_task(self.runner.run(job, normalized))
        self._tasks[job.id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job.id, None))
        return job
