"""Fail-closed, trusted-local LLM transport for meeting processing.

This module intentionally does not import any provider SDK.  A meeting job has
one provider route and one :class:`OutboundPrivacyGateway` transaction; a
failed request is surfaced to the durable worker rather than retried against a
different provider.
"""

from __future__ import annotations

import hashlib
import inspect
import math
import os
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlsplit

import httpx

from .outbound_privacy_service import EgressDescriptor, OutboundPrivacyGateway
# Keep one canonical error type for all meeting stages.  The worker owns the
# serialisable error contract; importing it here is safe because the worker
# does not import this module at module initialisation time.
from .meeting_processing_worker import MeetingProcessingStageError

_ALLOWED_MEETING_LOCAL_PROVIDERS = frozenset(
    {"openai_compatible_local", "ollama", "sglang"}
)
SOURCE_CHUNK_CHARS = 24_000
FINAL_SOURCE_CHARS = 48_000
MAX_REDUCTION_ROUNDS = 4
# Keep reduced transcript material bounded even when a long-running process
# handles many distinct jobs.  Entries are LRU-evicted and each value is
# already capped by ``FINAL_SOURCE_CHARS``.
REDUCTION_CACHE_MAX_ENTRIES = 32


@dataclass(frozen=True)
class MeetingLlmResult:
    markdown: str
    source_digest: str


def _cfg(config: Any, key: str, default: Any = None) -> Any:
    if isinstance(config, Mapping):
        value: Any = config
        for part in key.split("."):
            if not isinstance(value, Mapping) or part not in value:
                return default
            value = value[part]
        return value
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter(key, default)
            if value is not None:
                return value
        except TypeError:
            pass
    value = config
    for part in key.split("."):
        if isinstance(value, Mapping):
            value = value.get(part)
        else:
            value = getattr(value, part, None)
        if value is None:
            return default
    return value


def _configured(config: Any, env_name: str, key: str, default: Any = None) -> Any:
    env = os.getenv(env_name)
    if env is not None and env.strip() != "":
        return env.strip()
    value = _cfg(config, key, default)
    return default if value is None else value


def _normalise_base_url(value: Any) -> str:
    url = str(value or "").strip().rstrip("/")
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    # Credentials and fragments are never valid meeting endpoints.
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        return ""
    return url


@asynccontextmanager
async def _client_context(client: Any, *, timeout: float):
    """Use an injected client in tests, otherwise a strict httpx client."""
    if client is None:
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=timeout,
        ) as active:
            yield active
        return
    # Factories are useful for asserting constructor options without making a
    # network call.  A supplied client may itself be an async context manager.
    from_factory = callable(client)
    manager = client(timeout=timeout, follow_redirects=False) if from_factory else client
    active = manager
    entered = False
    if from_factory and hasattr(manager, "__aenter__"):
        active = await manager.__aenter__()
        entered = True
    try:
        yield active
    finally:
        if entered:
            await manager.__aexit__(None, None, None)


class MeetingLocalLlmService:
    """OpenAI-compatible local endpoint with no cloud/provider fallback."""

    def __init__(
        self,
        config: Any = None,
        *,
        gateway: OutboundPrivacyGateway | Any | None = None,
        privacy_gateway: OutboundPrivacyGateway | Any | None = None,
        http_client: Any | None = None,
        http_client_factory: Callable[..., Any] | None = None,
        timeout: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.config = config
        self.provider = str(
            _configured(config, "AOITALK_MEETING_LOCAL_LLM_PROVIDER", "meeting_processing.local_llm.provider", "openai_compatible_local")
            or "openai_compatible_local"
        ).strip().lower()
        self.model = str(
            _configured(config, "AOITALK_MEETING_LOCAL_LLM_MODEL", "meeting_processing.local_llm.model", "")
            or ""
        ).strip()
        configured_url = _configured(config, "AOITALK_MEETING_LOCAL_LLM_BASE_URL", "meeting_processing.local_llm.base_url", "")
        if not configured_url and self.provider == "openai_compatible_local":
            try:
                from ..llm.openai_compatible_local_profiles import openai_compatible_local_base_url

                configured_url = openai_compatible_local_base_url(config, model=self.model or None)
            except Exception:
                configured_url = ""
        self.base_url = _normalise_base_url(configured_url)
        self.timeout = self._finite_timeout(timeout if timeout is not None else _configured(config, "AOITALK_MEETING_LOCAL_LLM_TIMEOUT_SECONDS", "meeting_processing.local_llm.timeout_seconds", 30.0))
        if gateway is not None and privacy_gateway is not None and gateway is not privacy_gateway:
            raise ValueError("gateway and privacy_gateway are mutually exclusive")
        self.gateway = gateway or privacy_gateway or OutboundPrivacyGateway(config)
        self.http_client = http_client_factory or http_client
        self._reduced: OrderedDict[str, str] = OrderedDict()

    @staticmethod
    def _finite_timeout(value: Any) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 30.0
        return number if math.isfinite(number) and number > 0 else 30.0

    def _error(self, code: str, message: str, *, stage: str, retryable: bool = True, details: Mapping[str, Any] | None = None) -> MeetingProcessingStageError:
        return MeetingProcessingStageError(code, message, stage=stage, retryable=retryable, details=details)

    def _route_error(self, stage: str) -> MeetingProcessingStageError | None:
        if self.provider not in _ALLOWED_MEETING_LOCAL_PROVIDERS:
            return self._error("local_llm.route_not_local", "meeting processing LLM provider is not allowed", stage=stage)
        if not self.model:
            return self._error("local_llm.unavailable", "meeting processing LLM model is not configured", stage=stage)
        if not self.base_url:
            return self._error("local_llm.unavailable", "meeting processing LLM base URL is invalid", stage=stage)
        try:
            try:
                classification = self.gateway.provider_class(self.provider, base_url=self.base_url)
            except TypeError:
                # Keep simple test/embedding gateways with a positional-only
                # second argument compatible without weakening fail-closed
                # classification.
                classification = self.gateway.provider_class(self.provider, self.base_url)
        except Exception:
            classification = "unknown"
        if classification != "local":
            return self._error("local_llm.route_not_local", "meeting processing LLM route is not trusted-local", stage=stage)
        return None

    def _descriptor(self) -> EgressDescriptor:
        return EgressDescriptor(
            action="meeting.generate",
            transport="openai-compatible.chat.completions",
            destination=self.base_url,
            provider=self.provider,
            tool="meeting-processing",
            model=self.model,
        )

    async def _execute_http(self, payload: Mapping[str, Any], *, method: str, url: str) -> Any:
        async with _client_context(self.http_client, timeout=self.timeout) as client:
            async def sender(_final_payload: Any) -> Any:
                request = getattr(client, method.lower())
                if method == "GET":
                    return await request(url)
                return await request(url, json=_final_payload)

            result = self.gateway.execute(
                payload,
                provider=self.provider,
                descriptor=self._descriptor(),
                sender=sender,
                base_url=self.base_url,
                source_kind="meeting_processing",
                model=self.model,
            )
            return await result if inspect.isawaitable(result) else result

    @staticmethod
    def _json_response(response: Any) -> Mapping[str, Any] | None:
        if not response:
            return None
        try:
            status_code = int(getattr(response, "status_code", 0))
        except (TypeError, ValueError):
            return None
        if not 200 <= status_code < 300:
            return None
        try:
            value = response.json()
        except Exception:
            return None
        return value if isinstance(value, Mapping) else None

    @staticmethod
    def _model_ids(body: Mapping[str, Any]) -> set[str]:
        values = body.get("data", body.get("models", []))
        if not isinstance(values, list):
            return set()
        result: set[str] = set()
        for item in values:
            if isinstance(item, Mapping):
                value = item.get("id", item.get("name"))
                if isinstance(value, str):
                    result.add(value.strip())
        return result

    async def ready(self) -> bool:
        error = self._route_error("generating_minutes")
        if error is not None:
            return False
        try:
            response = await self._execute_http({"method": "GET", "path": "/models"}, method="GET", url=f"{self.base_url}/models")
            body = self._json_response(response)
            return bool(body is not None and self.model in self._model_ids(body))
        except Exception:
            return False

    async def readiness(self) -> bool:
        """Compatibility alias used by readiness composition roots."""
        return await self.ready()

    async def _complete(self, prompt: str, *, stage: str) -> str:
        error = self._route_error(stage)
        if error is not None:
            raise error
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "会議処理のためのローカル専用生成。Markdown本文だけを返してください。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "tools": [],
        }
        try:
            response = await self._execute_http(payload, method="POST", url=f"{self.base_url}/chat/completions")
        except MeetingProcessingStageError:
            raise
        except Exception as exc:
            raise self._error("local_llm.unavailable", "local LLM request failed", stage=stage, details={"type": type(exc).__name__}) from exc
        try:
            status = int(getattr(response, "status_code", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise self._error(
                "local_llm.unavailable",
                "local LLM endpoint returned an invalid status",
                stage=stage,
            ) from exc
        if not 200 <= status < 300:
            raise self._error(
                "local_llm.unavailable",
                "local LLM endpoint returned a non-success response",
                stage=stage,
                details={"status_code": status},
            )
        body = self._json_response(response)
        try:
            choices = body["choices"] if body is not None else None
            message = choices[0]["message"] if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], Mapping) else None
            content = message["content"] if isinstance(message, Mapping) else None
            if not isinstance(content, str):
                raise ValueError
            return content.strip()
        except Exception as exc:
            raise self._error("local_llm.invalid_response", "local LLM response was malformed", stage=stage) from exc

    async def _bounded_source(self, transcript: str, *, stage: str) -> tuple[str, str]:
        text = str(transcript or "")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        cached = self._reduced.get(digest)
        if cached is not None:
            self._reduced.move_to_end(digest)
            return cached, digest
        if len(text) <= FINAL_SOURCE_CHARS:
            self._reduced[digest] = text
            self._reduced.move_to_end(digest)
            while len(self._reduced) > REDUCTION_CACHE_MAX_ENTRIES:
                self._reduced.popitem(last=False)
            return text, digest
        reduced = text
        for _ in range(MAX_REDUCTION_ROUNDS):
            if len(reduced) <= FINAL_SOURCE_CHARS:
                break
            chunks = [reduced[i : i + SOURCE_CHUNK_CHARS] for i in range(0, len(reduced), SOURCE_CHUNK_CHARS)]
            notes = await self._reduce_chunks(chunks, stage=stage)
            reduced = "\n\n".join(notes)
        reduced = reduced[:FINAL_SOURCE_CHARS]
        self._reduced[digest] = reduced
        self._reduced.move_to_end(digest)
        while len(self._reduced) > REDUCTION_CACHE_MAX_ENTRIES:
            self._reduced.popitem(last=False)
        return reduced, digest

    @staticmethod
    def _coerce_transcript(value: Any) -> tuple[str, str]:
        """Accept the worker's structured payload as well as plain text."""
        title = ""
        if isinstance(value, Mapping):
            meeting = value.get("meeting")
            if isinstance(meeting, Mapping):
                title = str(meeting.get("title_hint") or meeting.get("title") or "")
            transcript = value.get("transcript", value.get("text", ""))
            if isinstance(transcript, Mapping):
                transcript = transcript.get("text", "")
            value = transcript
        return str(value or ""), title

    async def _reduce_chunks(self, chunks: list[str], *, stage: str) -> list[str]:
        output: list[str] = []
        for chunk in chunks:
            output.append(await self._complete("次の会議記録を事実/決定/宿題/論点の短い箇条書きに圧縮してください。\n\n" + chunk, stage=stage))
        return output

    async def generate_minutes(self, input_value: Any = None, *, transcript: Any = None, title_hint: str = "", **kwargs: Any) -> MeetingLlmResult:
        transcript_value = transcript if transcript is not None else kwargs.get("transcript", input_value)
        transcript, embedded_title = self._coerce_transcript(transcript_value)
        title_hint = title_hint or embedded_title
        source, digest = await self._bounded_source(transcript, stage="generating_minutes")
        markdown = await self._complete(f"議事録を作成してください。タイトル候補: {title_hint}\nsource_digest: {digest}\n\n{source}", stage="generating_minutes")
        return MeetingLlmResult(markdown=markdown, source_digest=digest)

    async def generate_memo(self, input_value: Any = None, *, transcript: Any = None, title_hint: str = "", source_digest: str | None = None, **kwargs: Any) -> MeetingLlmResult:
        transcript_value = transcript if transcript is not None else kwargs.get("transcript", input_value)
        transcript, embedded_title = self._coerce_transcript(transcript_value)
        title_hint = title_hint or embedded_title
        source, digest = await self._bounded_source(transcript, stage="generating_memo")
        if source_digest and source_digest != digest:
            raise self._error(
                "local_llm.invalid_response",
                "memo source digest does not match the transcript",
                stage="generating_memo",
                retryable=False,
                details={"expected_source_digest": digest},
            )
        markdown = await self._complete(f"議事メモを作成してください。タイトル候補: {title_hint}\nsource_digest: {digest}\n\n{source}", stage="generating_memo")
        return MeetingLlmResult(markdown=markdown, source_digest=digest)

    async def generate(self, artifact_type: str, transcript: Any = None, **kwargs: Any) -> MeetingLlmResult:
        """Small generic adapter for orchestration seams."""
        kind = str(artifact_type or "").strip().lower()
        if kind == "minutes":
            return await self.generate_minutes(transcript, **kwargs)
        if kind == "memo":
            return await self.generate_memo(transcript, **kwargs)
        raise self._error("local_llm.invalid_response", "unknown meeting artifact type", stage=f"generating_{kind or 'unknown'}", retryable=False)


__all__ = [
    "FINAL_SOURCE_CHARS",
    "MAX_REDUCTION_ROUNDS",
    "REDUCTION_CACHE_MAX_ENTRIES",
    "MeetingLocalLlmService",
    "MeetingLlmResult",
    "MeetingProcessingStageError",
    "SOURCE_CHUNK_CHARS",
    "_ALLOWED_MEETING_LOCAL_PROVIDERS",
]
