"""履歴やツールを変更しない一時的なLLMテキスト生成。"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import re
from typing import Any, Optional

from ..llm.generation_budget import GenerationBudget, current_generation_budget
from ..llm.generation_cancellation import GenerationCancelled
from ..llm.generation_error import ModelOutputIncompleteError

logger = logging.getLogger(__name__)

DEFAULT_EPHEMERAL_SYSTEM_PROMPT = (
    "You are a concise assistant. Follow the instruction exactly. "
    "Do not call tools or modify files. Return only the requested text."
)

_EPHEMERAL_METHOD_NAMES = (
    "generate_plain_text_async",
    "generate_memory_extraction_async",
)

EPHEMERAL_PROVIDER_EXCEPTION_CLASS_CODES = frozenset({
    "APITimeoutError", "APIConnectionError", "APIError", "APIStatusError",
    "AuthenticationError", "AttributeError", "BadRequestError", "CancelledError",
    "ConnectError", "ConnectTimeout", "GenerationCancelled", "HTTPStatusError",
    "FreeTeamUnavailableError", "InternalServerError", "KeyError", "NetworkError", "OpenAIError",
    "PermissionDeniedError", "PoolTimeout", "RateLimitError", "ReadError", "other",
    "ReadTimeout", "RemoteProtocolError", "RuntimeError", "TimeoutError",
    "TimeoutException", "TypeError", "ValueError", "WriteError", "WriteTimeout",
})
EPHEMERAL_PROVIDER_FAILURE_STAGES = frozenset({"before_request", "request", "response_parse"})


def safe_ephemeral_exception_class(exc: BaseException | type[BaseException] | str) -> str:
    """Return only an allowlisted exception class name, never its message."""

    if isinstance(exc, type):
        candidate = exc.__name__
    elif isinstance(exc, BaseException):
        candidate = type(exc).__name__
    else:
        candidate = str(exc)
    candidate = re.sub(r"[^A-Za-z0-9_]", "", candidate)[:40]
    return candidate if candidate in EPHEMERAL_PROVIDER_EXCEPTION_CLASS_CODES else "other"


def safe_ephemeral_exception_source_name(source: str | None) -> str:
    """Return only a basename:line pair suitable for diagnostics."""

    if not isinstance(source, str):
        return "other:0"
    filename, separator, line = source.rpartition(":")
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    if (
        not separator
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", filename)
        or not re.fullmatch(r"\d{1,6}", line)
    ):
        return "other:0"
    return f"{filename}:{line}"


def safe_ephemeral_exception_source(exc: BaseException | None) -> str:
    """Return the deepest traceback frame as a safe basename:line pair."""

    tb = getattr(exc, "__traceback__", None)
    if tb is None:
        return "other:0"
    while tb.tb_next is not None:
        tb = tb.tb_next
    filename = tb.tb_frame.f_code.co_filename.replace("\\", "/").rsplit("/", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", filename):
        filename = "other"
    return safe_ephemeral_exception_source_name(f"{filename}:{tb.tb_lineno}")


def _safe_free_team_reason_code(exc: BaseException) -> str:
    """Keep only a bounded lowercase reason token; never expose exception text."""

    args = getattr(exc, "args", ())
    if not isinstance(args, tuple) or len(args) != 1 or not isinstance(args[0], str):
        return "unavailable"
    reason = args[0].strip().casefold()
    if len(reason) > 48 or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", reason):
        return "unavailable"
    return reason


class EphemeralGenerationStopped(RuntimeError):
    """A stopped request is not model output eligible for format repair."""

    def __init__(self, status: str):
        self.status = status
        super().__init__(f"ephemeral_generation_{status}")


class EphemeralGenerationFailure(RuntimeError):
    """Fixed-code failure for callers that must distinguish errors from text."""

    _CATEGORIES = frozenset({"provider_unavailable", "timeout", "output_incomplete", "empty_response"})
    _CODES = frozenset({
        "http_402", "http_429", "http_5xx", "connection_error", "provider_exception", "unsupported_client",
        "quota_exhausted", "rate_limited", "truncated_length", "incomplete_output",
        "empty_response", "cancelled", "timeout", "free_team_budget_insufficient", "free_team_unavailable",
    })

    def __init__(
        self,
        category: str,
        code: str,
        *,
        response_char_count: int = 0,
        finish_reason: str | None = None,
        exception_class: str = "other",
        stage: str = "request",
        exception_source: str | None = None,
    ) -> None:
        self.category = category if category in self._CATEGORIES else "provider_unavailable"
        is_dynamic_free_team_code = (
            isinstance(code, str)
            and re.fullmatch(r"free_team_[a-z][a-z0-9_]{0,47}", code) is not None
        )
        self.code = (
            code
            if isinstance(code, str) and (code in self._CODES or is_dynamic_free_team_code)
            else "provider_exception"
        )
        self.response_char_count = max(0, min(int(response_char_count), 1_000_000))
        self.finish_reason = finish_reason if finish_reason in {"length", "max_output_tokens", "other"} else None
        self.exception_class = safe_ephemeral_exception_class(exception_class)
        self.stage = stage if stage in EPHEMERAL_PROVIDER_FAILURE_STAGES else "request"
        self.exception_source = safe_ephemeral_exception_source_name(exception_source)
        super().__init__("ephemeral_generation_failed")


def _safe_http_status(exc: BaseException) -> int | None:
    for value in (
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
        getattr(getattr(exc, "response", None), "status", None),
    ):
        if type(value) is int and 100 <= value <= 599:
            return value
    return None


def _fixed_failure(
    exc: BaseException,
    *,
    stage: str = "request",
) -> EphemeralGenerationFailure:
    exception_class = safe_ephemeral_exception_class(exc)
    exception_source = safe_ephemeral_exception_source(exc)
    if exception_class == "FreeTeamUnavailableError":
        reason = _safe_free_team_reason_code(exc)
        provider_code = (
            reason if reason.startswith("free_team_") else f"free_team_{reason}"
        )
        return EphemeralGenerationFailure(
            "provider_unavailable",
            provider_code,
            exception_class=exception_class,
            stage=stage,
            exception_source=exception_source,
        )
    if isinstance(exc, ModelOutputIncompleteError):
        reason = exc.reason.strip().lower()
        if reason in {"length", "max_output_tokens"}:
            return EphemeralGenerationFailure(
                "output_incomplete",
                "truncated_length",
                response_char_count=exc.response_char_count,
                finish_reason=reason,
                exception_class=exception_class,
                stage=stage,
                exception_source=exception_source,
            )
        if reason == "empty_output":
            return EphemeralGenerationFailure(
                "empty_response", "empty_response", response_char_count=exc.response_char_count,
                exception_class=exception_class, stage=stage, exception_source=exception_source,
            )
        return EphemeralGenerationFailure(
            "output_incomplete",
            "incomplete_output",
            response_char_count=exc.response_char_count,
            finish_reason="other",
            exception_class=exception_class,
            stage=stage,
            exception_source=exception_source,
        )
    exception_names = {base.__name__ for base in type(exc).__mro__ if isinstance(base.__name__, str)}
    if isinstance(exc, TimeoutError) or exception_names & {
        "APITimeoutError", "TimeoutException", "ConnectTimeout", "ReadTimeout",
        "WriteTimeout", "PoolTimeout",
    }:
        return EphemeralGenerationFailure(
            "timeout", "timeout", exception_class=exception_class, stage=stage,
            exception_source=exception_source,
        )
    error_code = getattr(exc, "code", None) or getattr(exc, "type", None)
    if isinstance(error_code, str):
        error_code = error_code.strip().lower()
        if error_code in {
            "insufficient_quota", "credit_balance_exhausted", "billing_hard_limit_reached", "quota_exceeded",
        }:
            return EphemeralGenerationFailure(
                "provider_unavailable", "quota_exhausted", exception_class=exception_class, stage=stage,
                exception_source=exception_source,
            )
        if error_code in {"rate_limit_exceeded", "requests", "tokens"}:
            return EphemeralGenerationFailure(
                "provider_unavailable", "rate_limited", exception_class=exception_class, stage=stage,
                exception_source=exception_source,
            )
    status = _safe_http_status(exc)
    if status == 402:
        return EphemeralGenerationFailure(
            "provider_unavailable", "http_402", exception_class=exception_class, stage=stage,
            exception_source=exception_source,
        )
    if status == 429:
        return EphemeralGenerationFailure(
            "provider_unavailable", "http_429", exception_class=exception_class, stage=stage,
            exception_source=exception_source,
        )
    if status is not None and 500 <= status <= 599:
        return EphemeralGenerationFailure(
            "provider_unavailable", "http_5xx", exception_class=exception_class, stage=stage,
            exception_source=exception_source,
        )
    if isinstance(exc, (ConnectionError, OSError)) or exception_names & {
        "APIConnectionError", "ConnectError", "ReadError", "WriteError",
        "NetworkError", "RemoteProtocolError",
    }:
        return EphemeralGenerationFailure(
            "provider_unavailable", "connection_error", exception_class=exception_class, stage=stage,
            exception_source=exception_source,
        )
    return EphemeralGenerationFailure(
        "provider_unavailable", "provider_exception", exception_class=exception_class, stage=stage,
        exception_source=exception_source,
    )


def agent_work_generation_timeout(config: Any) -> float:
    """Shared bounded budget; explicit ephemeral caller timeouts still win."""
    if isinstance(config, dict):
        section = config.get("ephemeral_llm", {})
        value = section.get("timeout_seconds", 600.0) if isinstance(section, dict) else 600.0
    else:
        value = config.get("ephemeral_llm.timeout_seconds", 600.0) if hasattr(config, "get") else 600.0
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return 600.0
    return min(1800.0, max(1.0, timeout)) if math.isfinite(timeout) else 600.0


def _get_explicit_method(llm_client: Any, name: str) -> Any:
    """``__getattr__``だけが作る動的属性を対応APIとみなさない。"""
    try:
        inspect.getattr_static(llm_client, name)
    except AttributeError:
        return None
    method = getattr(llm_client, name, None)
    return method if callable(method) else None


def supports_ephemeral_text(llm_client: Any) -> bool:
    """履歴非保存のテキスト生成APIを持つか判定する。"""

    if llm_client is None:
        return False
    return any(
        _get_explicit_method(llm_client, name) is not None
        for name in _EPHEMERAL_METHOD_NAMES
    )


def _method_accepts_keyword(method: Any, name: str) -> bool:
    try:
        parameters = inspect.signature(method).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        or parameter.name == name
        for parameter in parameters
    )


async def _invoke_ephemeral_method(
    method: Any,
    prompt: str,
    *,
    system_prompt: str,
    stage: list[str] | None = None,
) -> Optional[str]:
    kwargs = {}
    if _method_accepts_keyword(method, "system_prompt"):
        kwargs["system_prompt"] = system_prompt

    if stage is not None:
        stage[0] = "before_request"
    response = method(prompt, **kwargs)
    if stage is not None:
        stage[0] = "request"
    if inspect.isawaitable(response):
        response = await response
    if stage is not None:
        stage[0] = "response_parse"
    return str(response) if response else None


async def generate_ephemeral_text_with_llm_client(
    llm_client: Any,
    prompt: str,
    *,
    system_prompt: Optional[str] = None,
    timeout_seconds: Optional[float] = 20.0,
    provenance: Optional[dict[str, Any]] = None,
    max_requests: int | None = None,
    track_request_count: bool = False,
    raise_on_error: bool = False,
) -> Optional[str]:
    """生成するが、通常の会話履歴へ入力・出力を保存しない。

    通常の ``generate_async`` / ``generate_response_async`` は履歴保存を
    伴うため、ここでは明示的に副作用なしと契約したメソッドだけを呼ぶ。
    対応APIがない場合も、通常生成へフォールバックせず ``None`` を返す。
    """

    if llm_client is None:
        if raise_on_error:
            raise EphemeralGenerationFailure("provider_unavailable", "unsupported_client")
        return None

    effective_system_prompt = system_prompt or DEFAULT_EPHEMERAL_SYSTEM_PROMPT
    budget = GenerationBudget(
        timeout_seconds,
        max_requests=max_requests,
        track_request_count=track_request_count,
    )
    token = current_generation_budget.set(budget)
    status = "unsupported"
    current_stage = ["before_request"]

    async def _generate() -> Optional[str]:
        nonlocal status
        for method_name in _EPHEMERAL_METHOD_NAMES:
            method = _get_explicit_method(llm_client, method_name)
            if method is None:
                continue
            try:
                current_stage[0] = "before_request"
                budget.checkpoint()
                value = await _invoke_ephemeral_method(
                    method,
                    prompt,
                    system_prompt=effective_system_prompt,
                    stage=current_stage,
                )
                budget.checkpoint()
                status = "completed" if value else "empty"
                if not value and raise_on_error:
                    raise EphemeralGenerationFailure("empty_response", "empty_response")
                return value
            except EphemeralGenerationFailure:
                raise
            except (TimeoutError, GenerationCancelled):
                raise
            except Exception as exc:
                from ..llm.frozen_candidate_transport import current_frozen_transport
                from .agent_route_fallback_policy import RoutePreflightFailure
                if current_frozen_transport() is not None and isinstance(exc, RoutePreflightFailure):
                    status = "failed"
                    raise
                status = "failed"
                if raise_on_error:
                    failure = _fixed_failure(exc, stage=current_stage[0])
                    logger.warning(
                        "履歴非保存LLM呼び出しに失敗しました method=%s code=%s exception_class=%s stage=%s source=%s",
                        method_name,
                        failure.code,
                        failure.exception_class,
                        failure.stage,
                        failure.exception_source,
                    )
                    raise failure from None
                failure = _fixed_failure(exc, stage=current_stage[0])
                logger.warning(
                    "履歴非保存LLM呼び出しに失敗しました method=%s code=%s exception_class=%s stage=%s source=%s",
                    method_name,
                    failure.code,
                    failure.exception_class,
                    failure.stage,
                    failure.exception_source,
                )
                return None
        if raise_on_error:
            raise EphemeralGenerationFailure("provider_unavailable", "unsupported_client")
        logger.warning(
            "LLMクライアントが履歴非保存テキスト生成APIに対応していません: %s",
            type(llm_client).__name__,
        )
        return None

    try:
        if timeout_seconds is None:
            return await _generate()
        return await asyncio.wait_for(_generate(), timeout=timeout_seconds)
    except EphemeralGenerationFailure:
        status = "failed"
        raise
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except GenerationCancelled:
        status = "cancelled"
        if raise_on_error:
            raise EphemeralGenerationFailure(
                "provider_unavailable", "cancelled",
                exception_class="GenerationCancelled", stage=current_stage[0],
            ) from None
    except asyncio.TimeoutError:
        status = "timed_out"
        if raise_on_error:
            failure = _fixed_failure(asyncio.TimeoutError(), stage=current_stage[0])
            raise failure from None
        logger.warning("履歴非保存LLM呼び出しがタイムアウトしました stage=%s", current_stage[0])
    except Exception as exc:
        from ..llm.frozen_candidate_transport import current_frozen_transport
        from .agent_route_fallback_policy import RoutePreflightFailure
        if current_frozen_transport() is not None and isinstance(exc, RoutePreflightFailure):
            raise
        status = "failed"
        failure = _fixed_failure(exc, stage=current_stage[0])
        if raise_on_error:
            logger.warning(
                "履歴非保存LLM呼び出しに失敗しました code=%s exception_class=%s stage=%s source=%s",
                failure.code,
                failure.exception_class,
                failure.stage,
                failure.exception_source,
            )
            raise failure from None
        logger.warning(
            "履歴非保存LLM呼び出しに失敗しました code=%s exception_class=%s stage=%s source=%s",
            failure.code,
            failure.exception_class,
            failure.stage,
            failure.exception_source,
        )
    finally:
        # to_thread cannot be killed. Its copied context retains this event,
        # permanently fencing late responses and every subsequent retry.
        budget.cancelled.set()
        if provenance is not None:
            provenance.update(budget.provenance(status))
        current_generation_budget.reset(token)
    return None
