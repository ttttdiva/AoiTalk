"""Deterministic model bake-off contracts.

This module deliberately does not start an LLM server or download a model.
Callers provide the runner (and, when needed, an evaluator) callback.  The
service resolves the callback's route through AoiTalk's existing Execution
Profile/model routing and turns each callback result into a small, stable
metric record.  This makes a local fake runner and a production runner share
the same contract.

The public API is intentionally callback based:

* :class:`ModelBakeoffRunner` executes ``cases x models x repeats`` in the
  supplied order and supports synchronous or asynchronous callbacks.
* :class:`ModelBakeoffEvaluator` normalizes provider-specific result shapes.
* :func:`run_model_bakeoff` is a convenience wrapper for one report.

No model ID is selected by this module.  If ``models`` is omitted, the model
from the resolved Main route is used; otherwise every supplied model or route
specification is evaluated as-is.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, TypeVar

from .execution_profile_service import (
    resolve_execution_main_route,
    resolve_execution_profile_route,
)


MetricScalar = bool | int | float | str | None
RunnerOutput = Any
Clock = Callable[[], float]
RouteResolver = Callable[..., Mapping[str, Any] | None]
CallbackResult = TypeVar("CallbackResult")


class BakeoffRunnerCallback(Protocol):
    """Protocol accepted by :class:`ModelBakeoffRunner`.

    Implementations may be sync or async.  Named parameters are preferred;
    the runner filters the keyword set against the callback signature so
    legacy callbacks that only accept ``case`` and ``model`` remain usable.
    """

    def __call__(self, **kwargs: Any) -> RunnerOutput | Awaitable[RunnerOutput]: ...


class BakeoffEvaluatorCallback(Protocol):
    """Protocol for optional post-processing/evaluation callbacks."""

    def __call__(self, **kwargs: Any) -> RunnerOutput | Awaitable[RunnerOutput]: ...


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _as_string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, Mapping)):
        return tuple(text for item in value if (text := _as_text(item)))
    text = _as_text(value)
    return (text,) if text else ()


def _copy_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    # A shallow copy is deliberate: callback context is an opaque payload and
    # should not be mutated while the bake-off is being evaluated.
    return {str(key): item for key, item in value.items()}


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _non_negative_float(value: Any, default: float = 0.0) -> float:
    return max(0.0, _finite_float(value, default))


def _non_negative_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, number)


def _count_value(value: Any, default: int = 0) -> int:
    """Accept either a numeric count or a list of evidence items."""

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return len(value)
    return _non_negative_int(value, default)


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "y", "1", "ok", "pass", "passed", "success"}:
            return True
        if lowered in {"false", "no", "n", "0", "ng", "fail", "failed", "error"}:
            return False
    return bool(value)


def _first(mapping: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


@dataclass(frozen=True, slots=True, init=False)
class BakeoffCase:
    """One deterministic coding task used by the bake-off.

    ``id`` and ``task_id`` are accepted aliases for integrations that already
    have a task fixture.  ``expected_paths`` enables the built-in exploration
    precision/hallucinated-path derivation when the runner returns
    ``explored_paths``.
    """

    case_id: str
    prompt: str
    expected_tools: tuple[str, ...]
    expected_paths: tuple[str, ...]
    context: Mapping[str, Any]
    metadata: Mapping[str, Any]

    def __init__(
        self,
        case_id: str | None = None,
        prompt: str = "",
        *,
        id: str | None = None,
        task_id: str | None = None,
        expected_tools: Iterable[Any] | Any = (),
        expected_tool_calls: Iterable[Any] | Any | None = None,
        expected_paths: Iterable[Any] | Any = (),
        context: Mapping[str, Any] | None = None,
        context_snapshot: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        resolved_id = _as_text(case_id or id or task_id)
        if not resolved_id:
            raise ValueError("BakeoffCase requires a non-empty case_id (or id/task_id)")
        object.__setattr__(self, "case_id", resolved_id)
        object.__setattr__(self, "prompt", str(prompt or ""))
        if expected_tool_calls is not None:
            expected_tools = expected_tool_calls
        object.__setattr__(self, "expected_tools", _as_string_tuple(expected_tools))
        object.__setattr__(self, "expected_paths", _as_string_tuple(expected_paths))
        object.__setattr__(self, "context", _copy_mapping(context or context_snapshot))
        object.__setattr__(self, "metadata", _copy_mapping(metadata))

    @property
    def id(self) -> str:
        """Compatibility alias for ``case_id``."""

        return self.case_id

    @property
    def task_id(self) -> str:
        """Compatibility alias for task-oriented callers."""

        return self.case_id

    @property
    def scenario_id(self) -> str:
        """Alias used by scenario-oriented fixture loaders."""

        return self.case_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "id": self.case_id,
            "prompt": self.prompt,
            "expected_tools": list(self.expected_tools),
            "expected_paths": list(self.expected_paths),
            "context": dict(self.context),
            "metadata": dict(self.metadata),
        }

    to_dict = as_dict


@dataclass(frozen=True, slots=True, init=False)
class BakeoffMetrics:
    """Normalized metrics for one model/case attempt.

    The booleans are intentionally per-attempt values.  A report's model
    summary exposes the corresponding rates under ``*_rate`` while retaining
    the canonical names for JSON consumers.
    """

    tool_call_success: bool
    malformed_calls: int
    hallucinated_path: bool
    exploration_precision: float
    patch_success: bool
    test_success: bool
    build_success: bool
    director_required_fixes: int
    rounds: int
    latency_ms: float
    context: Mapping[str, Any]
    tool_call_attempts: int
    tool_call_successes: int

    def __init__(
        self,
        *,
        tool_call_success: Any = False,
        malformed_calls: Any = 0,
        hallucinated_path: Any = False,
        exploration_precision: Any = 0.0,
        patch_success: Any = False,
        test_success: Any = False,
        build_success: Any = False,
        director_required_fixes: Any = 0,
        rounds: Any = 0,
        latency_ms: Any = 0.0,
        context: Mapping[str, Any] | None = None,
        tool_call_attempts: Any = 0,
        tool_call_successes: Any = 0,
        **aliases: Any,
    ) -> None:
        if "tool_calls_success" in aliases:
            tool_call_success = aliases["tool_calls_success"]
        if "malformed_tool_calls" in aliases:
            malformed_calls = aliases["malformed_tool_calls"]
        if "hallucinated_paths" in aliases:
            hallucinated_path = aliases["hallucinated_paths"]
        if "exploration_precision_rate" in aliases:
            exploration_precision = aliases["exploration_precision_rate"]
        if "tests_success" in aliases:
            test_success = aliases["tests_success"]
        if "test_build_success" in aliases:
            test_success = aliases["test_build_success"]
            build_success = aliases["test_build_success"]
        if "director_required_fix_count" in aliases:
            director_required_fixes = aliases["director_required_fix_count"]
        if "round_count" in aliases:
            rounds = aliases["round_count"]
        object.__setattr__(self, "tool_call_success", bool(tool_call_success))
        object.__setattr__(self, "malformed_calls", _non_negative_int(malformed_calls))
        object.__setattr__(self, "hallucinated_path", bool(hallucinated_path))
        precision = min(1.0, max(0.0, _finite_float(exploration_precision)))
        object.__setattr__(self, "exploration_precision", precision)
        object.__setattr__(self, "patch_success", bool(patch_success))
        object.__setattr__(self, "test_success", bool(test_success))
        object.__setattr__(self, "build_success", bool(build_success))
        object.__setattr__(
            self,
            "director_required_fixes",
            _non_negative_int(director_required_fixes),
        )
        object.__setattr__(self, "rounds", _non_negative_int(rounds))
        object.__setattr__(self, "latency_ms", _non_negative_float(latency_ms))
        object.__setattr__(self, "context", _copy_mapping(context))
        object.__setattr__(self, "tool_call_attempts", _non_negative_int(tool_call_attempts))
        object.__setattr__(self, "tool_call_successes", _non_negative_int(tool_call_successes))

    @property
    def test_build_success(self) -> bool:
        """Whether both the test and build checks passed."""

        return self.test_success and self.build_success

    @property
    def malformed_tool_calls(self) -> int:
        return self.malformed_calls

    @property
    def hallucinated_paths(self) -> bool:
        return self.hallucinated_path

    @property
    def director_required_fix_count(self) -> int:
        return self.director_required_fixes

    @property
    def round_count(self) -> int:
        return self.rounds

    @property
    def latency(self) -> float:
        return self.latency_ms

    @property
    def tool_call_success_rate(self) -> float:
        if self.tool_call_attempts:
            return min(1.0, max(0.0, self.tool_call_successes / self.tool_call_attempts))
        return 1.0 if self.tool_call_success else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_call_success": self.tool_call_success,
            "tool_call_success_rate": self.tool_call_success_rate,
            "tool_call_attempts": self.tool_call_attempts,
            "tool_call_successes": self.tool_call_successes,
            "malformed_calls": self.malformed_calls,
            "hallucinated_path": self.hallucinated_path,
            "exploration_precision": self.exploration_precision,
            "patch_success": self.patch_success,
            "test_success": self.test_success,
            "build_success": self.build_success,
            "test_build_success": self.test_build_success,
            "director_required_fixes": self.director_required_fixes,
            "rounds": self.rounds,
            "latency_ms": self.latency_ms,
            "context": dict(self.context),
        }

    to_dict = as_dict


@dataclass(frozen=True, slots=True)
class BakeoffRunResult:
    """Auditable result for one ``model x case x repeat`` attempt."""

    model_id: str
    case_id: str
    success: bool
    route: Mapping[str, Any]
    metrics: BakeoffMetrics
    message: str = ""
    error: str | None = None
    output: Any = field(default=None, repr=False, compare=False)
    repeat: int = 1

    @property
    def latency_ms(self) -> float:
        return self.metrics.latency_ms

    @property
    def model(self) -> str:
        return self.model_id

    @property
    def context(self) -> Mapping[str, Any]:
        return self.metrics.context

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "case_id": self.case_id,
            "success": self.success,
            "route": dict(self.route),
            "metrics": self.metrics.as_dict(),
            "message": self.message,
            "error": self.error,
            "repeat": self.repeat,
        }

    to_dict = as_dict


@dataclass(frozen=True, slots=True)
class BakeoffModelSummary:
    """Aggregated metrics for one model."""

    model_id: str
    runs: int
    succeeded: int
    metrics: Mapping[str, Any]

    @property
    def success_rate(self) -> float:
        return _finite_float(self.metrics.get("success_rate"), 0.0)

    @property
    def model(self) -> str:
        return self.model_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "runs": self.runs,
            "succeeded": self.succeeded,
            "success_rate": self.success_rate,
            "metrics": dict(self.metrics),
        }

    to_dict = as_dict


@dataclass(frozen=True, slots=True)
class ModelBakeoffReport:
    """Stable, deterministic report returned by a bake-off run."""

    results: tuple[BakeoffRunResult, ...]
    summaries: tuple[BakeoffModelSummary, ...]
    routes: Mapping[str, Mapping[str, Any]]
    context: Mapping[str, Any]

    @property
    def model_ids(self) -> tuple[str, ...]:
        return tuple(summary.model_id for summary in self.summaries)

    @property
    def model_results(self) -> tuple[BakeoffRunResult, ...]:
        """Compatibility alias for consumers that call attempts model results."""

        return self.results

    @property
    def by_model(self) -> dict[str, tuple[BakeoffRunResult, ...]]:
        grouped: dict[str, list[BakeoffRunResult]] = {}
        for result in self.results:
            grouped.setdefault(result.model_id, []).append(result)
        return {model_id: tuple(items) for model_id, items in grouped.items()}

    @property
    def best_model(self) -> str | None:
        """Return the best deterministic candidate, or ``None`` for no runs."""

        if not self.summaries:
            return None

        def key(summary: BakeoffModelSummary) -> tuple[float, ... | str]:
            metrics = summary.metrics
            # Higher quality first, fewer Director fixes and lower latency
            # second; model ID is the final stable tie-breaker.
            return (
                -_finite_float(metrics.get("test_build_success_rate"), 0.0),
                -_finite_float(metrics.get("patch_success_rate"), 0.0),
                -_finite_float(metrics.get("tool_call_success_rate"), 0.0),
                -_finite_float(metrics.get("exploration_precision"), 0.0),
                _finite_float(metrics.get("director_required_fixes"), 0.0),
                _finite_float(metrics.get("latency_ms"), 0.0),
                summary.model_id,
            )

        return min(self.summaries, key=key).model_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "models": list(self.model_ids),
            "best_model": self.best_model,
            "context": dict(self.context),
            "routes": {key: dict(value) for key, value in self.routes.items()},
            "results": [item.as_dict() for item in self.results],
            "summaries": [item.as_dict() for item in self.summaries],
        }

    to_dict = as_dict


@dataclass(frozen=True, slots=True)
class ModelBakeoffConfig:
    """Optional immutable configuration object for :class:`ModelBakeoffRunner`."""

    cases: tuple[BakeoffCase, ...] = ()
    models: tuple[Any, ...] = ()
    repeats: int = 1
    context: Mapping[str, Any] = field(default_factory=dict)
    execution_profile_id: str | None = None
    subagent_id: str | None = None
    team_id: str | None = None


def _case_from_value(value: Any, index: int = 0) -> BakeoffCase:
    if isinstance(value, BakeoffCase):
        return value
    if isinstance(value, Mapping):
        return BakeoffCase(
            case_id=value.get("case_id") or value.get("id") or value.get("task_id") or f"case-{index + 1}",
            prompt=value.get("prompt") or value.get("instruction") or value.get("task") or "",
            expected_tools=value.get("expected_tools") or value.get("tools") or (),
            expected_paths=value.get("expected_paths") or value.get("paths") or (),
            context=value.get("context"),
            metadata=value.get("metadata"),
        )
    if isinstance(value, str):
        return BakeoffCase(case_id=value or f"case-{index + 1}", prompt=value)
    raise TypeError(f"unsupported bake-off case: {type(value).__name__}")


def normalize_bakeoff_cases(values: Iterable[Any]) -> tuple[BakeoffCase, ...]:
    """Normalize case fixtures without changing their input order."""

    return tuple(_case_from_value(value, index) for index, value in enumerate(values))


def _payload_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, BakeoffMetrics):
        return value.as_dict()
    if isinstance(value, BakeoffRunResult):
        payload = value.as_dict()
        payload.update(value.metrics.as_dict())
        return payload
    if isinstance(value, Mapping):
        result = dict(value)
        nested = result.get("metrics")
        if nested is not None and nested is not value:
            # Outer fields such as ``success`` and ``message`` remain visible;
            # evaluator-provided nested metrics override runner defaults.
            result.update(_payload_mapping(nested))
        return result
    if value is None:
        return {}
    names = (
        "success",
        "message",
        "error",
        "metrics",
        "tool_call_success",
        "malformed_calls",
        "hallucinated_path",
        "exploration_precision",
        "patch_success",
        "test_success",
        "build_success",
        "director_required_fixes",
        "rounds",
        "latency_ms",
        "context",
        "tool_calls",
        "explored_paths",
        "expected_paths",
    )
    result = {name: getattr(value, name) for name in names if hasattr(value, name)}
    nested = result.get("metrics")
    if nested is not None and nested is not value:
        result.update(_payload_mapping(nested))
    return result


def _path_key(value: Any) -> str:
    # Path is only a comparison key here; the original payload remains in the
    # callback result.  Normalize slashes to avoid platform-specific precision
    # differences in deterministic fixtures.
    text = _as_text(value).replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    return text.rstrip("/") or "/"


def _derive_tool_call_values(payload: Mapping[str, Any]) -> tuple[int, int, int, bool | None]:
    calls = payload.get("tool_calls")
    if not isinstance(calls, Sequence) or isinstance(calls, (str, bytes, bytearray)):
        return 0, 0, 0, None
    attempts = len(calls)
    successes = 0
    malformed = 0
    for call in calls:
        if not isinstance(call, Mapping):
            malformed += 1
            continue
        if bool(call.get("malformed") or call.get("parse_error")):
            malformed += 1
            continue
        status = _optional_bool(
            _first(call, "success", "succeeded", "ok", "passed", "valid")
        )
        if status is True:
            successes += 1
        elif status is None and not (call.get("error") or call.get("errors")):
            # A structurally valid call without a status is considered
            # attempted, not successful; explicit success is required.
            pass
    return attempts, successes, malformed, (successes == attempts if attempts else None)


def normalize_bakeoff_metrics(
    observation: Any,
    *,
    case: BakeoffCase | None = None,
    latency_ms: float = 0.0,
    context: Mapping[str, Any] | None = None,
) -> BakeoffMetrics:
    """Normalize a runner/evaluator payload into :class:`BakeoffMetrics`.

    Provider callbacks may return either a flat mapping, ``{"metrics": ...}``,
    or an object with matching attributes.  Unknown values are ignored rather
    than making one model invalidate an otherwise useful report.
    """

    payload = _payload_mapping(observation)
    base_context = _copy_mapping(context)
    raw_context = payload.get("context")
    if isinstance(raw_context, Mapping):
        base_context.update(_copy_mapping(raw_context))

    attempts, successes, malformed, derived_tool_success = _derive_tool_call_values(payload)
    attempts = _non_negative_int(
        _first(payload, "tool_call_attempts", "tool_calls_attempted")
        if _first(payload, "tool_call_attempts", "tool_calls_attempted") is not None
        else attempts
    )
    successes = _non_negative_int(
        _first(payload, "tool_call_successes", "tool_calls_succeeded")
        if _first(payload, "tool_call_successes", "tool_calls_succeeded") is not None
        else successes
    )
    malformed_value = _first(payload, "malformed_calls", "malformed_tool_calls")
    malformed = _count_value(malformed_value if malformed_value is not None else malformed)
    tool_success_value = _optional_bool(
        _first(payload, "tool_call_success", "tool_calls_success", "tools_success")
    )
    tool_success = derived_tool_success if tool_success_value is None else tool_success_value
    if tool_success is None:
        tool_success = successes > 0 and attempts == successes

    explored = _first(payload, "explored_paths", "exploration_paths", "visited_paths")
    explored_paths = _as_string_tuple(explored)
    expected_paths = case.expected_paths if case is not None else _as_string_tuple(payload.get("expected_paths"))
    expected_set = {_path_key(path) for path in expected_paths}
    explored_set = [_path_key(path) for path in explored_paths]
    outside_paths = {path for path in explored_set if path not in expected_set}
    hallucinated_value = _optional_bool(
        _first(payload, "hallucinated_path", "hallucinated_paths", "path_hallucination")
    )
    hallucinated = bool(outside_paths) if hallucinated_value is None else hallucinated_value
    precision_value = _first(payload, "exploration_precision", "exploration_precision_rate")
    exploration = payload.get("exploration")
    if precision_value is None and isinstance(exploration, Mapping):
        precision_value = _first(exploration, "precision", "exploration_precision")
    if precision_value is None:
        exploration_precision = (
            len({path for path in explored_set if path in expected_set}) / len(explored_set)
            if explored_set and expected_set
            else (1.0 if expected_set and not explored_set else 0.0)
        )
    else:
        exploration_precision = _finite_float(precision_value)

    patch_success = _optional_bool(
        _first(payload, "patch_success", "patch_applied", "patch_passed")
    )
    test_success = _optional_bool(
        _first(payload, "test_success", "tests_success", "test_passed", "tests_passed")
    )
    build_success = _optional_bool(
        _first(payload, "build_success", "build_passed", "build_ok")
    )
    combined = _optional_bool(_first(payload, "test_build_success", "tests_and_build_success"))
    if combined is not None:
        test_success = combined if test_success is None else test_success
        build_success = combined if build_success is None else build_success
    director_fixes = _first(
        payload,
        "director_required_fixes",
        "director_required_fix_count",
        "required_fixes",
        "review_required_fixes",
    )
    rounds = _first(payload, "rounds", "round_count", "turns")
    if isinstance(rounds, Sequence) and not isinstance(rounds, (str, bytes, bytearray)):
        rounds = len(rounds)
    explicit_latency = _first(payload, "latency_ms", "latency")
    measured_latency = _non_negative_float(
        explicit_latency if explicit_latency is not None else latency_ms
    )
    # Keep token/context counters in the context map even when a provider put
    # them at the top level.  This makes summaries useful without forcing a
    # provider-specific schema.
    for key in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "context_tokens",
        "context_size",
        "prompt_tokens",
    ):
        if key in payload and key not in base_context:
            base_context[key] = payload[key]

    return BakeoffMetrics(
        tool_call_success=bool(tool_success),
        malformed_calls=malformed,
        hallucinated_path=hallucinated,
        exploration_precision=exploration_precision,
        patch_success=bool(patch_success),
        test_success=bool(test_success),
        build_success=bool(build_success),
        director_required_fixes=_count_value(director_fixes or 0),
        rounds=rounds or 0,
        latency_ms=measured_latency,
        context=base_context,
        tool_call_attempts=attempts,
        tool_call_successes=successes,
    )


class ModelBakeoffEvaluator:
    """Normalize one callback observation and aggregate completed attempts."""

    def evaluate(
        self,
        observation: Any,
        *,
        model_id: str,
        case: BakeoffCase,
        route: Mapping[str, Any],
        latency_ms: float = 0.0,
        context: Mapping[str, Any] | None = None,
        repeat: int = 1,
    ) -> BakeoffRunResult:
        payload = _payload_mapping(observation)
        metrics = normalize_bakeoff_metrics(
            observation,
            case=case,
            latency_ms=latency_ms,
            context=context,
        )
        success_value = _optional_bool(_first(payload, "success", "completed", "passed"))
        if success_value is None:
            success_value = metrics.patch_success and metrics.test_build_success
        message = _as_text(_first(payload, "message", "summary", "final_message"))
        error_value = _first(payload, "error", "failure", "exception")
        error = _as_text(error_value) or None
        return BakeoffRunResult(
            model_id=_as_text(model_id),
            case_id=case.case_id,
            success=bool(success_value) and error is None,
            route=dict(route),
            metrics=metrics,
            message=message,
            error=error,
            output=observation,
            repeat=max(1, _non_negative_int(repeat, 1)),
        )

    __call__ = evaluate

    def summarize(
        self,
        model_id: str,
        results: Sequence[BakeoffRunResult],
    ) -> BakeoffModelSummary:
        runs = len(results)
        succeeded = sum(1 for item in results if item.success)

        def rate(values: Iterable[bool]) -> float:
            values = tuple(values)
            return sum(1 for value in values if value) / len(values) if values else 0.0

        def average(values: Iterable[float]) -> float:
            values = tuple(values)
            return sum(values) / len(values) if values else 0.0

        metrics_list = [item.metrics for item in results]
        contexts = [dict(item.context) for item in results]
        numeric_context: dict[str, list[float]] = {}
        for item in contexts:
            for key, value in item.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    numeric_context.setdefault(key, []).append(float(value))
        context_summary: dict[str, Any] = {
            "samples": contexts,
            "sample_count": len(contexts),
        }
        for key, values in numeric_context.items():
            context_summary[f"{key}_total"] = sum(values)
            context_summary[f"{key}_average"] = average(values)
        metrics: dict[str, Any] = {
            "success_rate": succeeded / runs if runs else 0.0,
            "tool_call_success_rate": rate(item.tool_call_success for item in metrics_list),
            "tool_call_success": rate(item.tool_call_success for item in metrics_list),
            "tool_call_attempts": sum(item.tool_call_attempts for item in metrics_list),
            "tool_call_successes": sum(item.tool_call_successes for item in metrics_list),
            "malformed_calls": sum(item.malformed_calls for item in metrics_list),
            "hallucinated_path_rate": rate(item.hallucinated_path for item in metrics_list),
            "hallucinated_path": rate(item.hallucinated_path for item in metrics_list),
            "exploration_precision": average(item.exploration_precision for item in metrics_list),
            "patch_success_rate": rate(item.patch_success for item in metrics_list),
            "patch_success": rate(item.patch_success for item in metrics_list),
            "test_success_rate": rate(item.test_success for item in metrics_list),
            "test_success": rate(item.test_success for item in metrics_list),
            "build_success_rate": rate(item.build_success for item in metrics_list),
            "build_success": rate(item.build_success for item in metrics_list),
            "test_build_success_rate": rate(item.test_build_success for item in metrics_list),
            "test_build_success": rate(item.test_build_success for item in metrics_list),
            "director_required_fixes": sum(
                item.director_required_fixes for item in metrics_list
            ),
            "director_required_fixes_average": average(
                item.director_required_fixes for item in metrics_list
            ),
            "rounds": average(item.rounds for item in metrics_list),
            "rounds_total": sum(item.rounds for item in metrics_list),
            "latency_ms": average(item.latency_ms for item in metrics_list),
            "latency_ms_total": sum(item.latency_ms for item in metrics_list),
            "context": context_summary,
        }
        return BakeoffModelSummary(
            model_id=_as_text(model_id),
            runs=runs,
            succeeded=succeeded,
            metrics=metrics,
        )


def _merge_observations(base: Any, evaluated: Any) -> Any:
    """Merge evaluator output over runner output without losing raw metrics."""

    if evaluated is None:
        return base
    left = _payload_mapping(base)
    right = _payload_mapping(evaluated)
    merged = dict(left)
    merged.update(right)
    # A nested evaluator metrics object should win over flat runner fields.
    if isinstance(evaluated, Mapping) and isinstance(evaluated.get("metrics"), Mapping):
        merged.update(dict(evaluated["metrics"]))
    return merged


def _callback_target(callback: Any, method_name: str) -> Callable[..., Any] | None:
    if callback is None:
        return None
    method = getattr(callback, method_name, None)
    if callable(method):
        return method
    if callable(callback):
        return callback
    raise TypeError(f"bake-off {method_name} callback must be callable")


def _callback_invocation(
    target: Callable[..., Any],
    values: Mapping[str, Any],
    *,
    fallback: tuple[Any, ...],
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Build a call without swallowing callback-internal ``TypeError``."""

    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return fallback, {}
    parameters = tuple(signature.parameters.values())
    accepts_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters)
    has_named_parameters = any(
        parameter.kind
        in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }
        for parameter in parameters
    )
    if not has_named_parameters:
        # ``def callback(*args)`` is a common tiny fake runner.  Give it the
        # documented positional fallback rather than silently dropping all
        # inputs.
        return fallback, {}
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    unresolved_required = False
    for parameter in parameters:
        if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
            continue
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            continue
        if parameter.name in values:
            if parameter.kind == inspect.Parameter.POSITIONAL_ONLY:
                args.append(values[parameter.name])
            else:
                kwargs[parameter.name] = values[parameter.name]
        elif parameter.default is inspect.Parameter.empty:
            unresolved_required = True
            break
    if unresolved_required:
        required_count = sum(
            parameter.kind
            in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            }
            and parameter.default is inspect.Parameter.empty
            for parameter in parameters
        )
        return fallback[:required_count] or fallback[:1], {}
    if accepts_kwargs:
        for key, value in values.items():
            kwargs.setdefault(key, value)
    return tuple(args), kwargs


async def _invoke_callback(
    callback: Any,
    values: Mapping[str, Any],
    *,
    method_name: str,
    fallback: tuple[Any, ...],
) -> Any:
    target = _callback_target(callback, method_name)
    if target is None:
        return None
    args, kwargs = _callback_invocation(target, values, fallback=fallback)
    result = target(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


def _normalise_model_specs(values: Any) -> tuple[Any, ...]:
    if values is None:
        return ()
    if isinstance(values, Mapping):
        specs: list[Any] = []
        for key, value in values.items():
            if isinstance(value, Mapping):
                item = dict(value)
                item.setdefault("model_id", key)
                item.setdefault("model", key)
                specs.append(item)
            else:
                specs.append({"model_id": key, "model": value or key})
        return tuple(specs)
    if isinstance(values, str):
        return (values,)
    if isinstance(values, Iterable):
        return tuple(values)
    return (values,)


def _model_spec_id(spec: Any, fallback: str) -> str:
    if isinstance(spec, Mapping):
        return _as_text(spec.get("model_id") or spec.get("id") or spec.get("model") or fallback)
    return _as_text(spec) or fallback


class ModelBakeoffRunner:
    """Execute deterministic cases against one or more routed models."""

    def __init__(
        self,
        config: Any | ModelBakeoffConfig | None = None,
        runner: BakeoffRunnerCallback | Any | None = None,
        *,
        evaluator: BakeoffEvaluatorCallback | Any | None = None,
        cases: Iterable[Any] | None = None,
        models: Iterable[Any] | Mapping[str, Any] | None = None,
        tasks: Iterable[Any] | None = None,
        model_ids: Iterable[Any] | Mapping[str, Any] | None = None,
        repeats: int | None = None,
        context: Mapping[str, Any] | None = None,
        execution_profile_id: str | None = None,
        subagent_id: str | None = None,
        team_id: str | None = None,
        route_resolver: RouteResolver | None = None,
        clock: Clock | None = None,
    ) -> None:
        if callable(config) and runner is None and not isinstance(config, ModelBakeoffConfig):
            runner, config = config, None
        self.config = config
        config_object = config if isinstance(config, ModelBakeoffConfig) else None
        configured_cases = (
            cases
            if cases is not None
            else (tasks if tasks is not None else (config_object.cases if config_object else ()))
        )
        configured_models = (
            models
            if models is not None
            else (
                model_ids
                if model_ids is not None
                else (config_object.models if config_object else ())
            )
        )
        self.cases = normalize_bakeoff_cases(configured_cases or ())
        self.models = _normalise_model_specs(configured_models)
        self.repeats = max(
            1,
            _non_negative_int(
                repeats if repeats is not None else (config_object.repeats if config_object else 1),
                1,
            ),
        )
        configured_context = config_object.context if config_object else {}
        self.context = {**_copy_mapping(configured_context), **_copy_mapping(context)}
        self.execution_profile_id = _as_text(
            execution_profile_id
            if execution_profile_id is not None
            else (config_object.execution_profile_id if config_object else "")
        ) or None
        self.subagent_id = _as_text(
            subagent_id if subagent_id is not None else (config_object.subagent_id if config_object else "")
        ) or None
        self.team_id = _as_text(
            team_id if team_id is not None else (config_object.team_id if config_object else "")
        ) or None
        self.runner = runner
        self.evaluator = evaluator
        self.route_resolver = route_resolver
        self.clock = clock or time.perf_counter

    def _resolve_route(self, spec: Any, case: BakeoffCase) -> tuple[str, dict[str, Any]]:
        main_route = dict(resolve_execution_main_route(self.config or {}))
        base_route = main_route
        if self.subagent_id:
            profile_route = None
            if self.team_id and self.execution_profile_id:
                # The regular resolver intentionally follows request-bound
                # session selection.  For offline bake-off scripts an
                # explicit team/profile pair is equally valid, so ask the
                # same canonical Team resolver directly without mutating a
                # ContextVar.
                from .agent_team_v3 import resolve_team_execution_route

                profile_route = resolve_team_execution_route(
                    self.config or {},
                    self.subagent_id,
                    team_id=self.team_id,
                    execution_profile_id=self.execution_profile_id,
                )
            if profile_route is None:
                profile_route = resolve_execution_profile_route(
                    self.config or {},
                    self.subagent_id,
                    main_route=main_route,
                )
            if profile_route:
                base_route = dict(profile_route)
        model_id = _model_spec_id(spec, _as_text(base_route.get("model")) or "configured-model")
        route = dict(base_route)
        if isinstance(spec, Mapping):
            supplied_route = spec.get("route")
            if isinstance(supplied_route, Mapping):
                route.update(dict(supplied_route))
            for key in (
                "provider",
                "model",
                "effort",
                "reasoning_effort",
                "backend",
                "base_url",
                "execution_profile_id",
            ):
                if key in spec:
                    route[key] = spec[key]
            model_id = _as_text(spec.get("model_id") or spec.get("id") or spec.get("model") or model_id)
        else:
            route["model"] = model_id
        if not _as_text(route.get("model")):
            route["model"] = model_id
        if self.execution_profile_id:
            route.setdefault("execution_profile_id", self.execution_profile_id)
        if self.team_id:
            route.setdefault("team_id", self.team_id)
        if self.route_resolver is not None:
            resolver_values = {
                "config": self.config,
                "model_id": model_id,
                "model": model_id,
                "case": case,
                "route": dict(route),
            }
            resolver_target = _callback_target(self.route_resolver, "resolve")
            assert resolver_target is not None
            resolver_args, resolver_kwargs = _callback_invocation(
                resolver_target,
                resolver_values,
                fallback=(model_id, dict(route)),
            )
            resolved = resolver_target(*resolver_args, **resolver_kwargs)
            if inspect.isawaitable(resolved):
                raise TypeError("route_resolver must be synchronous")
            if resolved:
                route = dict(resolved)
        route["model"] = _as_text(route.get("model")) or model_id
        route["bakeoff_model_id"] = model_id
        return model_id, route

    async def run(
        self,
        cases: Iterable[Any] | None = None,
        models: Iterable[Any] | Mapping[str, Any] | None = None,
        *,
        tasks: Iterable[Any] | None = None,
        model_ids: Iterable[Any] | Mapping[str, Any] | None = None,
    ) -> ModelBakeoffReport:
        """Run all attempts and return a report in stable input order."""

        selected_cases = cases if cases is not None else tasks
        selected_models = models if models is not None else model_ids
        normalized_cases = self.cases if selected_cases is None else normalize_bakeoff_cases(selected_cases)
        model_specs = self.models if selected_models is None else _normalise_model_specs(selected_models)
        if not model_specs:
            main_route = dict(resolve_execution_main_route(self.config or {}))
            configured_model = _as_text(main_route.get("model"))
            if configured_model:
                model_specs = (configured_model,)
            else:
                raise ValueError("models are required when the resolved Main route has no model")
        if not normalized_cases:
            raise ValueError("at least one bake-off case is required")

        results: list[BakeoffRunResult] = []
        routes: dict[str, Mapping[str, Any]] = {}
        for spec in model_specs:
            # Resolve once per model; the route resolver still receives every
            # case so a caller can intentionally vary route metadata by task.
            model_id_hint = _model_spec_id(spec, "configured-model")
            for case in normalized_cases:
                model_id, route = self._resolve_route(spec, case)
                routes.setdefault(model_id, dict(route))
                effective_context = dict(self.context)
                effective_context.update(_copy_mapping(case.context))
                for repeat in range(1, self.repeats + 1):
                    started = self.clock()
                    observation: Any
                    error: Exception | None = None
                    if self.runner is None:
                        observation = {
                            "success": False,
                            "error": "bake-off runner callback is not configured",
                        }
                    else:
                        try:
                            observation = await _invoke_callback(
                                self.runner,
                                {
                                    "case": case,
                                    "task": case,
                                    "scenario": case,
                                    "work_item": case,
                                    "case_id": case.case_id,
                                    "model": model_id,
                                    "model_id": model_id,
                                    "model_name": model_id,
                                    "selected_model": model_id,
                                    "route": dict(route),
                                    "prompt": case.prompt,
                                    "context": dict(effective_context),
                                    "attempt": repeat,
                                    "repeat": repeat,
                                    "config": self.config,
                                },
                                method_name="run",
                                fallback=(case, model_id, dict(route)),
                            )
                        except Exception as exc:  # callback failure is a metric row, not a crash
                            error = exc
                            observation = {
                                "success": False,
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                    elapsed_ms = _non_negative_float((self.clock() - started) * 1000.0)
                    if error is None and self.evaluator is not None:
                        try:
                            evaluated = await _invoke_callback(
                                self.evaluator,
                                {
                                    "observation": observation,
                                    "result": observation,
                                    "output": observation,
                                    "case": case,
                                    "task": case,
                                    "scenario": case,
                                    "work_item": case,
                                    "case_id": case.case_id,
                                    "model": model_id,
                                    "model_id": model_id,
                                    "model_name": model_id,
                                    "selected_model": model_id,
                                    "route": dict(route),
                                    "context": dict(effective_context),
                                    "latency_ms": elapsed_ms,
                                    "attempt": repeat,
                                    "repeat": repeat,
                                    "config": self.config,
                                },
                                method_name="evaluate",
                                fallback=(observation, case, dict(route)),
                            )
                            observation = _merge_observations(observation, evaluated)
                        except Exception as exc:
                            error = exc
                            observation = _merge_observations(
                                observation,
                                {
                                    "success": False,
                                    "error": f"evaluator {type(exc).__name__}: {exc}",
                                },
                            )
                    if error is not None:
                        observation = _merge_observations(
                            observation,
                            {"success": False, "error": f"{type(error).__name__}: {error}"},
                        )
                    result = ModelBakeoffEvaluator().evaluate(
                        observation,
                        model_id=model_id,
                        case=case,
                        route=route,
                        latency_ms=elapsed_ms,
                        context=effective_context,
                        repeat=repeat,
                    )
                    results.append(result)

        evaluator = ModelBakeoffEvaluator()
        summaries: list[BakeoffModelSummary] = []
        seen_models: set[str] = set()
        for result in results:
            if result.model_id in seen_models:
                continue
            seen_models.add(result.model_id)
            summaries.append(
                evaluator.summarize(
                    result.model_id,
                    [item for item in results if item.model_id == result.model_id],
                )
            )
        return ModelBakeoffReport(
            results=tuple(results),
            summaries=tuple(summaries),
            routes={key: dict(value) for key, value in routes.items()},
            context=dict(self.context),
        )

    async def run_async(
        self,
        cases: Iterable[Any] | None = None,
        models: Iterable[Any] | Mapping[str, Any] | None = None,
        *,
        tasks: Iterable[Any] | None = None,
        model_ids: Iterable[Any] | Mapping[str, Any] | None = None,
    ) -> ModelBakeoffReport:
        """Explicit async alias for callers that prefer ``run_async``."""

        return await self.run(cases=cases, models=models, tasks=tasks, model_ids=model_ids)

    def run_sync(
        self,
        cases: Iterable[Any] | None = None,
        models: Iterable[Any] | Mapping[str, Any] | None = None,
        *,
        tasks: Iterable[Any] | None = None,
        model_ids: Iterable[Any] | Mapping[str, Any] | None = None,
    ) -> ModelBakeoffReport:
        """Synchronous adapter for scripts and deterministic unit tests."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.run(cases=cases, models=models, tasks=tasks, model_ids=model_ids)
            )
        raise RuntimeError("run_sync cannot be called from a running event loop; await run()")

    __call__ = run


# Names used by early bake-off callers.  Keep them aliases rather than
# separate implementations so metric semantics cannot drift.
ModelBakeoff = ModelBakeoffRunner
BakeoffRunner = ModelBakeoffRunner
BakeoffEvaluator = ModelBakeoffEvaluator
BakeoffTask = BakeoffCase
ModelBakeoffCase = BakeoffCase
BakeoffScenario = BakeoffCase
ModelBakeoffScenario = BakeoffCase
BakeoffResult = BakeoffRunResult
BakeoffSummary = BakeoffModelSummary
BakeoffReport = ModelBakeoffReport
ModelBakeoffMetrics = BakeoffMetrics
ModelBakeoffRun = BakeoffRunResult
ModelBakeoffResult = BakeoffRunResult
ModelBakeoffSummary = BakeoffModelSummary
DeterministicBakeoffRunner = ModelBakeoffRunner


async def run_model_bakeoff(
    cases: Iterable[Any] | None = None,
    models: Iterable[Any] | Mapping[str, Any] | None = None,
    *,
    tasks: Iterable[Any] | None = None,
    model_ids: Iterable[Any] | Mapping[str, Any] | None = None,
    config: Any | ModelBakeoffConfig | None = None,
    runner: BakeoffRunnerCallback | Any | None = None,
    evaluator: BakeoffEvaluatorCallback | Any | None = None,
    repeats: int = 1,
    context: Mapping[str, Any] | None = None,
    execution_profile_id: str | None = None,
    subagent_id: str | None = None,
    team_id: str | None = None,
    route_resolver: RouteResolver | None = None,
    clock: Clock | None = None,
) -> ModelBakeoffReport:
    """Run a bake-off without constructing :class:`ModelBakeoffRunner`."""

    return await ModelBakeoffRunner(
        config,
        runner,
        evaluator=evaluator,
        cases=cases,
        models=models,
        tasks=tasks,
        model_ids=model_ids,
        repeats=repeats,
        context=context,
        execution_profile_id=execution_profile_id,
        subagent_id=subagent_id,
        team_id=team_id,
        route_resolver=route_resolver,
        clock=clock,
    ).run()


def run_model_bakeoff_sync(*args: Any, **kwargs: Any) -> ModelBakeoffReport:
    """Synchronous convenience wrapper around :func:`run_model_bakeoff`."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(run_model_bakeoff(*args, **kwargs))
    raise RuntimeError("run_model_bakeoff_sync cannot run inside an event loop")


async def run_bakeoff(*args: Any, **kwargs: Any) -> ModelBakeoffReport:
    """Compatibility alias for :func:`run_model_bakeoff`."""

    return await run_model_bakeoff(*args, **kwargs)


def evaluate_bakeoff_observation(
    observation: Any,
    *,
    case: BakeoffCase | Mapping[str, Any] | None = None,
    model_id: str = "",
    route: Mapping[str, Any] | None = None,
    latency_ms: float = 0.0,
    context: Mapping[str, Any] | None = None,
) -> BakeoffRunResult:
    """Evaluate one callback payload without executing a runner."""

    normalized_case = _case_from_value(case, 0) if case is not None else BakeoffCase("case-1")
    return ModelBakeoffEvaluator().evaluate(
        observation,
        model_id=model_id or "configured-model",
        case=normalized_case,
        route=route or {},
        latency_ms=latency_ms,
        context=context,
    )


def evaluate_model_bakeoff(*args: Any, **kwargs: Any) -> BakeoffRunResult:
    """Compatibility alias for :func:`evaluate_bakeoff_observation`."""

    return evaluate_bakeoff_observation(*args, **kwargs)


__all__ = [
    "BakeoffCase",
    "BakeoffEvaluator",
    "BakeoffEvaluatorCallback",
    "BakeoffMetrics",
    "BakeoffReport",
    "BakeoffResult",
    "BakeoffRunner",
    "BakeoffRunnerCallback",
    "BakeoffSummary",
    "BakeoffTask",
    "BakeoffScenario",
    "DeterministicBakeoffRunner",
    "ModelBakeoff",
    "ModelBakeoffCase",
    "ModelBakeoffConfig",
    "ModelBakeoffEvaluator",
    "ModelBakeoffMetrics",
    "ModelBakeoffReport",
    "ModelBakeoffResult",
    "ModelBakeoffRun",
    "ModelBakeoffRunner",
    "ModelBakeoffScenario",
    "ModelBakeoffSummary",
    "BakeoffRunResult",
    "BakeoffModelSummary",
    "evaluate_bakeoff_observation",
    "evaluate_model_bakeoff",
    "normalize_bakeoff_cases",
    "normalize_bakeoff_metrics",
    "run_bakeoff",
    "run_model_bakeoff",
    "run_model_bakeoff_sync",
]
