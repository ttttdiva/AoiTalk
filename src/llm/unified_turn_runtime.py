"""Unified turn runtime primitives shared by provider adapters.

The runtime keeps the provider-specific transport at the edges and owns the
agent loop shape: model output -> tool execution -> tool result input ->
follow-up sampling.  It is intentionally small, but it gives OpenAI-style,
CLI-style, and specialist runners the same execution contract.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterator, Mapping, Sequence

from ..tools.core import (
    ToolArgumentValidationError,
    ToolPolicyDeniedError,
    argument_correlation,
)
from ..tools.registry import ToolRegistry
from ..services.agent_team_service import (
    ToolFailureCircuitBreaker,
    is_internal_tool_failure_code,
    parse_structured_tool_failure,
    tool_failure_family,
    tool_failure_scope,
)
from ..services.agent_team_v3 import agent_team_v3_delegation_enabled
from ..services.semantic_citation import safe_verified_media_read_url
from .context_compression import model_tool_result_payload
from .generation_cancellation import (
    GenerationInterrupted,
    PlanningInteractionTerminated,
)
from .generation_error import (
    GenerationFailure,
    tool_failure_circuit_open_failure,
    tool_execution_blocked_failure,
)
from .turn_stream_events import (
    SyncStreamEmitter,
    emit_assistant_text,
    emit_thinking,
    thinking_text_from_message,
)

logger = logging.getLogger(__name__)

# A CLI provider can stop after producing a tool marker which was not
# executed (for example when the follow-up process fails or the round budget
# is exhausted).  Keep this payload deliberately provider-neutral and free of
# the raw marker so callers that do not have their own failure mapping (such
# as specialist delegation) cannot persist/display an executable-looking
# pseudo tool call as the assistant answer.
CLI_TOOL_LOOP_FAILURE_MESSAGE = (
    "ツール処理を完了できませんでした。"
    "未完了の操作は実行していないため、内容を確認してから再実行してください。"
)
DEFAULT_CLI_TOOL_CONTEXT_MAX_CHARS = 32_000
MAX_CLI_TOOL_CONTEXT_MAX_CHARS = 64_000
UNKNOWN_TOOL_NAME = "unknown_tool"


def _follow_up_tool_choice(value: Any) -> Any:
    """Relax a one-shot required tool choice for the next model turn.

    OpenAI-compatible providers encode a required function either as the
    string ``"required"`` or as a function-selection mapping.  The latter
    must be reset too; otherwise a successful `/search` (or another explicit
    command) can keep forcing the same function on every follow-up round.
    """

    if value == "required":
        return "auto"
    if isinstance(value, Mapping):
        choice_type = str(value.get("type") or "").casefold()
        function = value.get("function")
        if choice_type == "function" and isinstance(function, Mapping):
            if str(function.get("name") or "").strip():
                return "auto"
    return value


def tool_output_indicates_success(output: Any) -> tuple[bool, str]:
    """Interpret structured tool failures instead of treating every return as success."""
    text = str(output).strip()
    payload: Any = output
    if not isinstance(payload, Mapping):
        try:
            payload = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = None
    if isinstance(payload, Mapping) and payload.get("success") is False:
        return False, str(payload.get("error") or "tool reported failure")
    lowered = text.casefold()
    if lowered.startswith(("error:", "tool execution error:", "tool not found")):
        return False, text
    return True, ""


_COMMAND_TOOL_NAMES = frozenset(
    {"execute_command", "read_command_output", "write_command_input", "stop_command"}
)
_COMMAND_RESULT_TEXT_LIMIT = 1_500
_COMMAND_RESULT_TOTAL_LIMIT = 12_000
_TOOL_END_OUTPUT_LIMIT = 4_096
_MEDIA_READ_EVENT_OUTPUT_LIMIT = 4_096
_MEDIA_READ_EVENT_CITATION_KEYS = frozenset(
    {
        "body_sha256",
        "derived_text_start",
        "derived_text_end",
        "text_start",
        "text_end",
        "text_length",
        "chunk_id",
        "chunk_index",
        "page_number",
        "start_seconds",
        "end_seconds",
        "unit_id",
    }
)
_MEDIA_READ_EVENT_LOCATOR_KEYS = frozenset(
    {"kind", "id", "session_id", "docs_library_id"}
)
_MEDIA_READ_EVENT_FIELDS = (
    "success",
    "complete",
    "target_kind",
    "source_kind",
    "source_id",
    "unit_id",
    "session_id",
    "message_id",
    "original_url",
    "conversation_url",
)


def _is_command_tool_name(value: Any) -> bool:
    return str(value or "").rsplit(".", 1)[-1].strip().lower() in _COMMAND_TOOL_NAMES


def _tool_result_output(tool_name: str, result: Any) -> str:
    """Keep command failures structured so exit evidence reaches the model."""

    if _is_command_tool_name(tool_name) and isinstance(result, Mapping):
        try:
            return json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            pass
    return str(result)


def _bounded_media_read_event_output(result: "UnifiedToolResult") -> str:
    """Project a media read for stream/audit consumers without source text.

    The model still receives the normal ``model_tool_result_payload`` below.
    ``tool_end`` crosses a separate persistence/WebSocket boundary, so a
    successful canonical read must carry only the fields needed to make a
    source link and prove that the read was complete.  In particular, never
    copy ``text`` (or an opaque provider output) into this event projection.
    """

    if str(result.call.tool or "").rsplit(".", 1)[-1].strip().casefold() != "media_read":
        return result.model_output

    try:
        payload = json.loads(result.output)
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = None
    if not isinstance(payload, Mapping):
        return json.dumps(
            {"success": False, "complete": False},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    projected: dict[str, Any] = {}
    for key in _MEDIA_READ_EVENT_FIELDS:
        if key in {"original_url", "conversation_url"}:
            continue
        value = payload.get(key)
        if key in {"success", "complete"}:
            if isinstance(value, bool):
                projected[key] = value
            continue
        if isinstance(value, str) and value and len(value) <= 2_048:
            if not any(ord(char) < 32 or ord(char) == 127 for char in value):
                projected[key] = value

    # Do not infer authority from a missing producer marker.  The persistence
    # projection is fail-closed even when a legacy/native reader omits it.
    if "success" not in projected:
        projected["success"] = False
    if "complete" not in projected:
        projected["complete"] = False

    arguments = result.call.arguments if isinstance(result.call.arguments, Mapping) else {}
    target_kind = str(
        projected.get("target_kind")
        or arguments.get("target_kind")
        or "media"
    ).strip()
    if target_kind and len(target_kind) <= 64:
        projected["target_kind"] = target_kind

    locator = arguments.get("locator")
    if isinstance(locator, Mapping):
        safe_locator: dict[str, str] = {}
        for key in _MEDIA_READ_EVENT_LOCATOR_KEYS:
            value = locator.get(key)
            if isinstance(value, str) and value and len(value) <= 256:
                if not any(ord(char) < 32 or ord(char) == 127 for char in value):
                    safe_locator[key] = value
        if safe_locator:
            projected["locator"] = safe_locator
            projected.setdefault("source_kind", safe_locator.get("kind"))
            projected.setdefault("source_id", safe_locator.get("id"))

    if "unit_id" not in projected:
        unit_id = arguments.get("unit_id")
        if isinstance(unit_id, str) and unit_id and len(unit_id) <= 256:
            projected["unit_id"] = unit_id

    citation = payload.get("citation")
    if isinstance(citation, Mapping):
        safe_citation: dict[str, Any] = {}
        for key in _MEDIA_READ_EVENT_CITATION_KEYS:
            value = citation.get(key)
            if isinstance(value, (bool, int, float)) and not isinstance(value, bool):
                safe_citation[key] = value
            elif isinstance(value, str) and value and len(value) <= 256:
                if not any(ord(char) < 32 or ord(char) == 127 for char in value):
                    safe_citation[key] = value
        if safe_citation:
            projected["citation"] = safe_citation

    # Only a strictly successful, complete read with the required canonical
    # body digest may carry a link across the emitter boundary.  This keeps a
    # malformed/no-source success envelope from becoming a transport citation.
    citation_hash = citation.get("body_sha256") if isinstance(citation, Mapping) else None
    requires_hash = target_kind.casefold() in {"docs", "knowledge", "conversation"}
    warnings = payload.get("warnings")
    canonical_empty_warnings = isinstance(warnings, list) and not warnings
    can_emit_urls = (
        projected.get("success") is True
        and projected.get("complete") is True
        and (not requires_hash or (isinstance(citation_hash, str) and re.fullmatch(r"[0-9a-fA-F]{64}", citation_hash)))
        and (
            canonical_empty_warnings
            or (not requires_hash and "warnings" not in payload)
        )
    )
    if can_emit_urls:
        for key in ("original_url", "conversation_url"):
            safe_url = safe_verified_media_read_url(
                payload.get(key),
                target_kind=target_kind,
                locator=locator if isinstance(locator, Mapping) else None,
                unit_id=projected.get("unit_id"),
                source_id=projected.get("source_id"),
            )
            if safe_url:
                projected[key] = safe_url

    if "warnings" in payload:
        if not isinstance(warnings, list):
            projected["warnings"] = ["unverified"]
        else:
            invalid_warning = False
            safe_warnings = []
            for item in warnings[:8]:
                if (
                    not isinstance(item, str)
                    or len(item) > 200
                    or any(ord(char) < 32 or ord(char) == 127 for char in item)
                ):
                    invalid_warning = True
                    continue
                safe_warnings.append(item)
            if invalid_warning:
                projected["warnings"] = ["unverified"]
            elif safe_warnings:
                projected["warnings"] = safe_warnings

    # Keep malformed/failed errors bounded for the existing UI, while never
    # copying the full tool body into the durable event.
    if not projected.get("success"):
        error = payload.get("error")
        if isinstance(error, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", error):
            projected["error"] = error

    rendered = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
    if len(rendered) <= _MEDIA_READ_EVENT_OUTPUT_LIMIT:
        return rendered
    # This should only be reachable for unexpectedly large identifiers or
    # warning lists.  Preserve the proof/link fields and drop optional data.
    compact: dict[str, Any] = {
        key: projected[key]
        for key in (
            "success",
            "complete",
            "target_kind",
            "source_kind",
            "source_id",
            "unit_id",
            "locator",
        )
        if key in projected
    }
    # Keep the body digest and at most one server URL when the optional
    # metadata made the first rendering unexpectedly large.
    citation = projected.get("citation")
    if isinstance(citation, Mapping):
        body_sha256 = citation.get("body_sha256")
        if isinstance(body_sha256, str) and len(body_sha256) <= 128:
            compact["citation"] = {"body_sha256": body_sha256}
    for key in ("original_url", "conversation_url"):
        value = projected.get(key)
        if not isinstance(value, str):
            continue
        candidate = dict(compact)
        candidate[key] = value
        rendered = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
        if len(rendered) <= _MEDIA_READ_EVENT_OUTPUT_LIMIT:
            return rendered
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


def _bounded_tool_end_text(value: Any) -> str:
    """Redact and bound generic tool text before it reaches tool_end."""

    try:
        from ..services.outbound_privacy_service import redact_secret_for_local_display

        redacted = redact_secret_for_local_display(str(value or ""))
    except Exception:
        return "[tool output omitted: redaction unavailable]"
    if not isinstance(redacted, str):
        return "[tool output omitted: redaction unavailable]"
    if len(redacted) <= _TOOL_END_OUTPUT_LIMIT:
        return redacted
    return redacted[: _TOOL_END_OUTPUT_LIMIT - 16].rstrip() + "\n... (truncated)"


def _bounded_tool_end_output(result: "UnifiedToolResult") -> str:
    if str(result.call.tool or "").rsplit(".", 1)[-1].strip().casefold() == "media_read":
        return _bounded_media_read_event_output(result)
    return _bounded_tool_end_text(result.model_output)


def _redact_command_text(value: Any, *, limit: int = _COMMAND_RESULT_TEXT_LIMIT) -> str:
    """Bound command streams before exposing them to a model-facing result."""

    try:
        from ..services.local_agent_tasks import SecretRedactor

        return SecretRedactor().text(value, limit)
    except Exception:
        # A missing redactor must never turn a command result into a secret
        # disclosure.  The structured exit code is retained separately.
        return "[command output omitted: redaction unavailable]"


def _command_result_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return the bounded, redacted command contract used by model output."""

    result: dict[str, Any] = {}
    for key in (
        "success", "return_code", "stdout", "stderr", "output", "error",
        "job_id", "status", "next_offset", "truncated", "timed_out",
        "cancelled", "finalization_error", "finalization_published",
    ):
        if key not in value:
            continue
        item = value.get(key)
        if key in {"stdout", "stderr", "output", "error", "finalization_error"}:
            if item is not None:
                result[key] = _redact_command_text(item)
        elif key == "return_code":
            if isinstance(item, int) and not isinstance(item, bool):
                result[key] = max(-1_000_000, min(item, 1_000_000))
        elif isinstance(item, (bool, int, float)) or item is None:
            result[key] = item
        elif key in {"job_id", "status"}:
            result[key] = _redact_command_text(item, limit=200)
    return result


def _command_nonzero_failure(value: Any) -> bool:
    """Recognize a ToolGuard command exit only with numeric/result agreement."""

    if not isinstance(value, Mapping) or value.get("success") is not False:
        return False
    return_code = value.get("return_code")
    if isinstance(return_code, bool) or not isinstance(return_code, int) or return_code == 0:
        return False
    if value.get("timed_out") is True or value.get("cancelled") is True:
        return False
    if "status" in value and str(value.get("status") or "").strip().lower() not in {"", "exited", "completed"}:
        return False
    if value.get("finalization_error"):
        return False
    if "finalization_published" in value and value.get("finalization_published") is False:
        return False
    if value.get("error_code"):
        return False
    error = str(value.get("error") or "").strip()
    match = re.fullmatch(r"Command exited with code (-?\d+)", error)
    if match:
        return int(match.group(1)) == return_code
    # The plain execute_command executor reports a nonzero shell exit with the
    # process stderr as ``error`` (no "Command exited" sentence).  Accept that
    # shape only when the error is exactly the captured stderr, so a wrapper or
    # scope denial that merely carries some other text stays fail-closed.
    stderr = value.get("stderr")
    if error and isinstance(stderr, str) and error == stderr.strip():
        return True
    # A test runner such as pytest reports failures on stdout and leaves stderr
    # empty, so execute_command falls back to this fixed sentence.  It only
    # counts as an ordinary exit when it names the same numeric return code.
    return error == f"コマンドが終了コード {return_code} で失敗しました"


def _is_file_mutation_tool_name(value: Any) -> bool:
    """A successful file-editing call.  Echo/read/other commands are not progress."""

    from .tool_policy import FILESYSTEM_MUTATION_TOOL_NAMES

    name = str(value or "").rsplit(".", 1)[-1].strip().lower()
    if name in _COMMAND_TOOL_NAMES:
        # Commands appear in the policy's mutation set, but a command run
        # (echo, read, test) proves no file changed.
        return False
    return name in {
        str(item).rsplit(".", 1)[-1].casefold() for item in FILESYSTEM_MUTATION_TOOL_NAMES
    }


_DURATION_UNIT = r"(?:ms|msec|milliseconds?|s|secs?|seconds?|m|mins?|minutes?)"
# Only known timing/timestamp decorations: whole ``Wall time:``/``Duration:``
# lines, per-test duration lines, "in 10.9s"-style phrases, pytest's
# ``(0:02:54)`` total, bracketed ``[0.12s]`` and clock/ISO timestamps.
# Other numbers (ids, counts, values) are substantive and are kept.
_TIMING_NOISE_RES = (
    re.compile(r"^[ \t]*(?:wall[ _]time|duration|elapsed(?: time)?)\b.*$", re.I | re.M),
    re.compile(rf"^[ \t]*\d+(?:\.\d+)?[ \t]*{_DURATION_UNIT}[ \t]+(?:call|setup|teardown)\b.*$", re.I | re.M),
    re.compile(
        rf"\b(?:in|took|after|elapsed)[ \t]+\d+(?:\.\d+)?[ \t]*{_DURATION_UNIT}\b"
        r"(?:[ \t]*\(\d+:\d{2}:\d{2}\))?",
        re.I,
    ),
    re.compile(rf"[\[(]\d+(?:\.\d+)?[ \t]*{_DURATION_UNIT}[\])]", re.I),
    re.compile(
        r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
    ),
    re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\b"),
)
_FAILED_TEST_LINE_RE = re.compile(r"^FAILED\s+(\S+)", re.M)


def _stable_command_outcome(output: Any) -> str:
    """Semantic outcome of a command exit: return code plus failing ids or text.

    Known timing/timestamp decorations vary between identical failures and are
    excluded so a changed ``Wall time`` cannot evade the guard.  Substantive
    numbers (ids, counts, values) stay: a different value is a different outcome.
    """

    try:
        payload = json.loads(output) if isinstance(output, str) else dict(output or {})
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, Mapping):
        payload = {}
    text = "\n".join(str(payload.get(key) or "") for key in ("stdout", "stderr"))
    failed = sorted(set(_FAILED_TEST_LINE_RE.findall(text)))
    if failed:
        body = "\n".join(failed)
    else:
        body = text
        for pattern in _TIMING_NOISE_RES:
            body = pattern.sub("", body)
        body = re.sub(r"\s+", " ", body).strip()
    return f"{payload.get('return_code')}|{body}"


def _observable_tool_arguments(
    registry: ToolRegistry,
    tool_name: str,
    arguments: Any,
) -> dict[str, Any]:
    """Return only arguments accepted by the canonical tool contract.

    Provider/model arguments are untrusted.  The router is authoritative for
    execution, but turn callbacks and audit consumers must not receive the raw
    rejected payload (which may contain credentials or other sensitive fields).
    Normalize aliases such as Docs ``project_id`` to their canonical key and
    emit an empty object when validation fails or a definition is unavailable.
    """

    try:
        definition = registry.get(str(tool_name or ""))
    except Exception:
        definition = None
    if definition is None:
        return {}
    normalizer = getattr(definition, "normalize_arguments", None)
    if not callable(normalizer):
        return {}
    try:
        normalized = normalizer(
            arguments if isinstance(arguments, dict) else {}
        )
    except Exception:
        # Observation must never leak rejected raw arguments or interfere with
        # the router's authoritative failure result.
        return {}
    normalized = dict(normalized or {})
    for name in getattr(definition, "hidden_argument_names", ()) or ():
        normalized.pop(str(name), None)
    return normalized


def observable_tool_name(registry: ToolRegistry, tool_name: Any) -> str:
    """Return a bounded public name for a provider-supplied tool marker.

    Registered tool names are repository-controlled and remain unchanged.
    Unknown provider names are reduced to one stable token before they reach
    events, audit records, logs, or model-facing result objects.
    """

    candidate = str(tool_name or "").strip()
    try:
        definition = registry.get(candidate)
    except Exception:
        definition = None
    if definition is not None:
        return candidate
    # Legacy embedders may expose only ``__contains__`` + ``execute`` rather
    # than the full ToolRegistry definition API. Preserve those trusted names
    # for execution while keeping genuinely unknown provider markers bounded.
    try:
        if candidate and candidate in registry:
            return candidate
    except Exception:
        pass
    return UNKNOWN_TOOL_NAME


def _registry_contains(registry: ToolRegistry, tool_name: str) -> bool:
    """Compatibility membership check for legacy registry adapters."""

    try:
        return bool(tool_name) and tool_name in registry
    except Exception:
        return False


_SAFE_EXCEPTION_CODE_RE = re.compile(r"[a-z][a-z0-9_]{2,63}")


def _safe_exception_code(exc: BaseException) -> str:
    """Return the fixed reason code an exception explicitly opts to expose."""

    code = getattr(exc, "safe_code", None)
    return code if isinstance(code, str) and _SAFE_EXCEPTION_CODE_RE.fullmatch(code) else ""


@dataclass(frozen=True)
class UnifiedToolCall:
    """Provider-neutral tool call."""

    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""
    raw: Any = None


@dataclass
class ToolNoProgressGuard:
    """Stop one unchanged validation/parse failure within a logical turn.

    The guard intentionally covers only errors where the model can make one
    concrete correction.  Framework/tool-domain failures continue through the
    existing failure breaker and approval/policy blocks remain authoritative.
    """

    max_same_failure: int = 2
    _counts: dict[str, int] = field(default_factory=dict, init=False)
    _success_digests: dict[str, set[str]] = field(default_factory=dict, init=False)
    _exit_counts: dict[str, int] = field(default_factory=dict, init=False)
    _effect_epoch: int = field(default=0, init=False)

    def note_effect(self) -> None:
        """Record a successful file mutation; only that changes what a rerun sees."""

        self._effect_epoch += 1

    def observe_command_exit(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any] | None,
        output: Any,
    ) -> GenerationFailure | None:
        """Stop only an identical command that exits identically with no file edit between.

        A nonzero exit is task evidence, not an infrastructure fault, so a
        changed command, a changed outcome, or a successful file mutation in
        between counts as progress and is never stopped here.
        """

        try:
            rendered = json.dumps(
                {
                    "arguments": dict(arguments or {}),
                    "outcome": _stable_command_outcome(output),
                    "epoch": self._effect_epoch,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except (TypeError, ValueError):
            rendered = "{}"
        digest = hashlib.sha256(rendered.encode("utf-8", "replace")).hexdigest()[:16]
        key = f"{self._normalized_tool_name(tool_name)}:command_exit:{digest}"
        count = self._exit_counts.get(key, 0) + 1
        self._exit_counts[key] = count
        if count < max(1, int(self.max_same_failure)):
            return None
        return tool_failure_circuit_open_failure(
            family=tool_failure_family(tool_name),
            signature=f"no_progress_command_exit_{digest}",
        )

    @staticmethod
    def _normalized_tool_name(tool_name: str) -> str:
        return re.sub(
            r"[^a-z0-9_.:-]+",
            "_",
            str(tool_name or UNKNOWN_TOOL_NAME).strip().lower(),
        )[:128] or UNKNOWN_TOOL_NAME

    def _canonical_key(
        self,
        tool_name: str,
        arguments: Mapping[str, Any] | None,
        error_code: str,
        failure_group: str | None = None,
    ) -> tuple[str, str]:
        try:
            rendered = json.dumps(
                dict(arguments or {}),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except (TypeError, ValueError):
            rendered = "{}"
        if str(failure_group or "").strip().lower().startswith("correctable_once"):
            # Planning owns one correction opportunity for the whole
            # preflight/revision scope; changing invalid args or error codes
            # must not mint another budget slot.
            digest = "correction"
        else:
            digest = hashlib.sha256(
                rendered.encode("utf-8", "replace")
            ).hexdigest()[:16]
        normalized_tool = self._normalized_tool_name(tool_name)
        normalized_code = re.sub(
            r"[^a-z0-9_.:-]+",
            "_",
            str(failure_group or error_code or "tool_argument_invalid")
            .strip()
            .lower(),
        ) or "tool_argument_invalid"
        normalized_code = normalized_code[:64]
        return f"{normalized_tool}:{normalized_code}:{digest}", normalized_code

    def observe_failure(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any] | None,
        error_code: str,
        failure_group: str | None = None,
    ) -> GenerationFailure | None:
        """Record a repairable failure and return a terminal failure on repeat.

        ``failure_group`` lets a typed domain outcome declare that several
        allowlisted codes share one concrete correction opportunity.
        """

        key, normalized_code = self._canonical_key(
            tool_name,
            arguments,
            error_code,
            failure_group,
        )
        count = self._counts.get(key, 0) + 1
        self._counts[key] = count
        if count < max(1, int(self.max_same_failure)):
            return None
        family = tool_failure_family(tool_name)
        return tool_failure_circuit_open_failure(
            family=family,
            signature=f"no_progress_{normalized_code}_{key.rsplit(':', 1)[-1]}",
        )

    def record_progress(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any] | None,
        output: Any,
    ) -> bool:
        """Reset this tool after a new, bounded success observation.

        Only the digest is retained. Repeating an identical read result does
        not provide new progress and therefore cannot reset an unchanged
        failure indefinitely.
        """

        tool = self._normalized_tool_name(tool_name)
        try:
            rendered = json.dumps(
                {
                    "arguments": dict(arguments or {}),
                    "result": str(output or ""),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except (TypeError, ValueError):
            rendered = "{}"
        digest = hashlib.sha256(
            rendered.encode("utf-8", "replace")
        ).hexdigest()[:16]
        seen_digests = self._success_digests.setdefault(tool, set())
        if digest in seen_digests:
            return False
        seen_digests.add(digest)
        prefix = f"{tool}:"
        self._counts = {
            key: count
            for key, count in self._counts.items()
            if not key.startswith(prefix)
        }
        return True


_NO_PROGRESS_VALIDATION_CODES = frozenset(
    {
        "not_found",
        "ambiguous_target",
        "user_validation",
        "validation",
        "docs_not_found",
        "docs_ambiguous_target",
        "docs_validation",
        "invalid_search_request",
        "invalid_read_request",
        "invalid_locator",
        "conversation_context_required",
        "query_source_budget_exceeded",
        "evidence_changed",
        "evidence_version_required",
        "tool_argument_unknown",
        "tool_argument_conflict",
        "tool_argument_missing",
        "tool_argument_invalid",
    }
)


# Failures that are neither an infrastructure fault nor something a changed call
# can fix: a command's own nonzero exit is task evidence and cancellation /
# circuit markers are framework outcomes.  They never feed the unchanged-call guard.
_NO_PROGRESS_EXEMPT_CODES = frozenset(
    {"command_failed", "command_nonzero_exit", "circuit_open", "not_executed", "cancelled"}
)


def _is_correctable_tool_failure(error_code: str) -> bool:
    code = str(error_code or "").strip().lower()
    return code not in _NO_PROGRESS_EXEMPT_CODES and not is_internal_tool_failure_code(code)


_MODEL_SAFE_DETAIL_CODES = frozenset({"read_invalid_line_range"})


_ACTIVE_NO_PROGRESS_GUARD: ContextVar[ToolNoProgressGuard | None] = ContextVar(
    "aoitalk_tool_no_progress_guard",
    default=None,
)


def current_tool_no_progress_guard() -> ToolNoProgressGuard | None:
    """Return the request/logical-turn guard bound to this execution context."""

    return _ACTIVE_NO_PROGRESS_GUARD.get()


@contextmanager
def activate_tool_no_progress_guard(
    guard: ToolNoProgressGuard | None = None,
) -> Iterator[ToolNoProgressGuard]:
    """Bind one guard across provider/review continuations of one logical turn."""

    active = guard or current_tool_no_progress_guard() or ToolNoProgressGuard()
    token = _ACTIVE_NO_PROGRESS_GUARD.set(active)
    try:
        yield active
    finally:
        _ACTIVE_NO_PROGRESS_GUARD.reset(token)


@dataclass(frozen=True)
class UnifiedToolResult:
    """Provider-neutral tool execution result."""

    call: UnifiedToolCall
    output: str
    success: bool
    error: str = ""
    elapsed_ms: float = 0.0
    # Policy blocks have a user-safe reason even when ``error`` is
    # intentionally empty for legacy model-facing payloads.  Keep that
    # structured reason available to stream/audit projections without making
    # the result text itself the status authority.
    blocked_reason: str = ""
    # A framework-owned failure is terminal for the enclosing turn.  Keep it
    # separate from provider/tool output so raw text cannot manufacture one.
    generation_failure: GenerationFailure | None = None
    # Framework-owned, user-safe status text for stream/UI projections.
    status_message: str = ""
    # Typed framework outcomes can stop the current turn without pretending
    # to be a generation transport failure (for example approval is pending).
    turn_terminal: bool = False
    terminal_reason: str = ""
    failure_group: str = ""
    # Framework-owned execution provenance; raw tool output cannot set it.
    executed: bool = True
    synthetic: bool = False
    # Digest (never the value) of the restored raw intent, so durable receipts
    # recorded from the observable projection stay comparable by identity.
    intent_digest: str = ""
    # Safe, framework-authored provenance of a server-contract argument
    # rejection (code and target digest only; never argument values).
    argument_rejection: Mapping[str, str] | None = None
    # Fixed framework policy code for a refusal that happened before execution.
    policy_denial: Mapping[str, str] | None = None

    @property
    def model_output(self) -> str:
        if _is_command_tool_name(self.call.tool):
            try:
                payload = json.loads(self.output)
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, Mapping):
                bounded = _command_result_projection(payload)
                prefix = ""
                if not self.success:
                    prefix = (
                        "Error: "
                        + _redact_command_text(
                            self.error or payload.get("error") or "command failed",
                            limit=1_000,
                        )
                        + "\n"
                    )
                rendered = prefix + json.dumps(bounded, ensure_ascii=False, default=str)
                if len(rendered) > _COMMAND_RESULT_TOTAL_LIMIT:
                    rendered = rendered[:_COMMAND_RESULT_TOTAL_LIMIT]
                return rendered
            fallback = _redact_command_text(self.error or self.output, limit=_COMMAND_RESULT_TOTAL_LIMIT)
            return ("Error: " if not self.success else "") + fallback
        if self.success:
            return self.output
        detail = self._safe_failure_detail()
        return f"Error: {self.error or self.output}" + (f" ({detail})" if detail else "")

    def _safe_failure_detail(self) -> str:
        """Fixed-vocabulary detail the model needs to correct its own call.

        Only codes whose ``message`` is built from the caller's integers and
        fixed text are surfaced; arbitrary tool messages stay out of the
        model-facing line (``_read_failure`` keeps ``error`` secret-free).
        """

        try:
            payload = json.loads(self.output)
        except (TypeError, ValueError, json.JSONDecodeError):
            return ""
        if not isinstance(payload, Mapping):
            return ""
        if str(payload.get("error_code") or "") not in _MODEL_SAFE_DETAIL_CODES:
            return ""
        message = payload.get("message")
        if not isinstance(message, str):
            return ""
        return re.sub(r"\s+", " ", message).strip()[:200]


@dataclass(frozen=True)
class UnifiedTurnResult:
    """Final result of a unified turn loop."""

    final_output: str
    tool_results: list[UnifiedToolResult] = field(default_factory=list)
    rounds: int = 0
    stopped_reason: str = "final"
    messages: list[dict[str, Any]] = field(default_factory=list)
    # ``tool_results`` is the model/display view and may contain synthetic
    # same-batch results.  Audit consumers must use this execution-only view.
    audit_tool_results: list[UnifiedToolResult] = field(default_factory=list)
    generation_failure: GenerationFailure | None = None

    @property
    def tool_calls(self) -> list[UnifiedToolCall]:
        return [result.call for result in self.tool_results]


def _tool_status_message(
    result: UnifiedToolResult,
    *,
    success_message: str,
) -> str:
    """Return a safe UI status without promoting raw tool output to authority."""

    if result.status_message:
        return result.status_message
    if not result.success:
        return "ツール実行に失敗しました"
    return success_message


def _planning_tool_outcome_projection(value: Any) -> dict[str, Any] | None:
    """Project only the typed planning outcome into a safe tool result."""

    try:
        from ..services.planning_tool_outcome import is_planning_tool_outcome

        if not is_planning_tool_outcome(value):
            return None
    except Exception:
        return None
    code = str(getattr(value, "failure_code", "") or "").strip().lower()
    failure_class = str(getattr(value, "failure_class", "") or "").strip().lower()
    if failure_class not in {"terminal", "correctable_once", "human_wait"}:
        return None
    message = str(value.get("message") or "").strip()
    if not code or not message:
        return None
    revision = getattr(value, "revision", None)
    if revision is None and isinstance(value.get("planning_outcome"), Mapping):
        revision = value["planning_outcome"].get("revision")
    revision_text = str(revision or "").strip()
    if not re.fullmatch(r"[1-9][0-9]{0,8}", revision_text):
        revision_text = ""
    try:
        rendered = json.dumps(dict(value), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        rendered = json.dumps(
            {
                "success": False,
                "error_code": code,
                "message": message,
            },
            ensure_ascii=False,
        )
    return {
        "output": rendered,
        "success": False,
        "error": code,
        "status_message": message[:512],
        "turn_terminal": failure_class in {"terminal", "human_wait"},
        "terminal_reason": code,
        "failure_group": (
            (
                "correctable_once"
                + (f":{revision_text}" if revision_text else "")
            )
            if failure_class == "correctable_once"
            else ""
        ),
    }

@dataclass
class UnifiedTurnLedger:
    """Mutable tool-result ledger shared by retries of one logical turn."""

    run_id: str | None = None
    results: list[UnifiedToolResult] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    # One immutable receipt per provider invocation.  The key is generated at
    # the provider boundary and makes retries/replayed callbacks idempotent
    # without collapsing two distinct rounds that happen to report the same
    # token counts.
    usage_records: dict[str, dict[str, Any]] = field(default_factory=dict)
    usage_unknown: bool = False
    reserved_tokens: int = 0
    reserved_cost_micros: int = 0
    failure: str = ""
    on_result: Callable[[UnifiedToolResult], None] | None = None
    _provider_sequence: int = field(default=0, init=False, repr=False)
    _budget_lock: threading.RLock = field(
        default_factory=threading.RLock,
        init=False,
        repr=False,
    )
    output_includes_reasoning: bool | None = None
    total_tokens_source: str | None = None

    def record(self, result: UnifiedToolResult) -> None:
        self.results.append(result)
        if self.on_result is not None:
            self.on_result(result)

    def next_provider_usage_key(self, prefix: str = "provider") -> str:
        """Return a unique request key local to this logical turn."""

        with self._budget_lock:
            self._provider_sequence += 1
            return f"{prefix}:{self._provider_sequence}"

    def mark_usage_unknown(self) -> None:
        """Fence later budget admissions after a provider omits usage."""

        with self._budget_lock:
            self.usage_unknown = True

    def reserve_provider_budget(
        self,
        tokens: int,
        *,
        total_tokens: int | None = None,
        total_cost_micros: int | None = None,
        cost_micros: int = 0,
    ) -> str | None:
        """Reserve output capacity before a concurrent provider starts.

        The check and increment share one lock.  This is the admission fence
        that prevents two parallel nested specialists from observing the same
        stale remainder and both starting an over-budget request.
        """

        try:
            amount = max(0, int(tokens))
        except (TypeError, ValueError, OverflowError):
            amount = 0
        if amount <= 0:
            return None
        with self._budget_lock:
            if total_tokens is not None:
                consumed = int(self.usage.get("total_tokens", 0) or 0)
                if consumed + int(self.reserved_tokens or 0) + amount > int(total_tokens):
                    return None
            if total_cost_micros is not None:
                consumed_cost = int(self.usage.get("cost_micros", 0) or 0)
                try:
                    requested_cost = max(0, int(cost_micros))
                except (TypeError, ValueError, OverflowError):
                    requested_cost = 0
                if (
                    consumed_cost
                    + int(self.reserved_cost_micros or 0)
                    + requested_cost
                    > int(total_cost_micros)
                ):
                    return None
            key = self.next_provider_usage_key("reservation")
            self.reserved_tokens += amount
            if cost_micros:
                self.reserved_cost_micros += max(0, int(cost_micros))
            self._remember_provider_reservation(
                key,
                amount,
                max(0, int(cost_micros or 0)),
            )
            return key

    def settle_provider_budget(
        self,
        reservation_key: str | None,
        usage: Mapping[str, Any] | None = None,
    ) -> None:
        """Release one in-flight output reservation exactly once."""

        if not reservation_key:
            return
        # Reservations are tracked by key on the ledger so a duplicate
        # callback cannot release another invocation's capacity.
        with self._budget_lock:
            reservations = getattr(self, "_reservations", None)
            if reservations is None:
                reservations = {}
                object.__setattr__(self, "_reservations", reservations)
            reservation = reservations.pop(reservation_key, 0)
            if isinstance(reservation, tuple):
                amount, cost = reservation
            else:
                amount, cost = reservation, 0
            self.reserved_tokens = max(0, self.reserved_tokens - int(amount or 0))
            self.reserved_cost_micros = max(
                0,
                self.reserved_cost_micros - int(cost or 0),
            )

    def _remember_provider_reservation(
        self,
        key: str,
        amount: int,
        cost_micros: int = 0,
    ) -> None:
        with self._budget_lock:
            reservations = getattr(self, "_reservations", None)
            if reservations is None:
                reservations = {}
                object.__setattr__(self, "_reservations", reservations)
            reservations[key] = (
                max(0, int(amount)),
                max(0, int(cost_micros or 0)),
            )

    def record_usage(
        self,
        usage: Mapping[str, Any] | None,
        *,
        usage_key: str | None = None,
    ) -> None:
        """Accumulate confirmed usage from every completion in this turn."""

        if not isinstance(usage, Mapping):
            return
        normalized: dict[str, Any] = {}
        for key in (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cached_tokens",
            "total_tokens",
            "cost_micros",
        ):
            value = usage.get(key)
            if value is None:
                continue
            try:
                normalized[key] = max(0, int(value))
            except (TypeError, ValueError):
                continue
        if not normalized:
            return
        reported_total = usage.get("total_tokens")
        if usage.get("total_tokens_source") == "derived":
            # A normalized synthetic total is an accounting projection, not
            # a provider receipt. Reconstruct it from the canonical flags so
            # a stale input+output value cannot erase separate reasoning.
            reported_total = None
        output_includes_reasoning = usage.get("output_includes_reasoning")
        if isinstance(output_includes_reasoning, bool):
            normalized["output_includes_reasoning"] = output_includes_reasoning
        if reported_total is None:
            normalized["total_tokens"] = (
                normalized.get("input_tokens", 0)
                + normalized.get("output_tokens", 0)
                + (
                    normalized.get("reasoning_tokens", 0)
                    if output_includes_reasoning is not True
                    else 0
                )
            )
            normalized["total_tokens_source"] = "derived"
        else:
            normalized["total_tokens_source"] = str(
                usage.get("total_tokens_source") or "reported"
            )
        with self._budget_lock:
            key = str(usage_key or "").strip() or self.next_provider_usage_key()
            if key in self.usage_records:
                return
            self.usage_records[key] = dict(normalized)
            for name, value in normalized.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                self.usage[name] = self.usage.get(name, 0) + value
            if isinstance(output_includes_reasoning, bool):
                if self.output_includes_reasoning is None:
                    self.output_includes_reasoning = output_includes_reasoning
                elif self.output_includes_reasoning != output_includes_reasoning:
                    self.output_includes_reasoning = None
            source = normalized.get("total_tokens_source")
            if isinstance(source, str):
                if self.total_tokens_source is None:
                    self.total_tokens_source = source
                elif self.total_tokens_source != source:
                    self.total_tokens_source = "mixed"
            if self.output_includes_reasoning is not None:
                self.usage["output_includes_reasoning"] = self.output_includes_reasoning
            if self.total_tokens_source is not None:
                self.usage["total_tokens_source"] = self.total_tokens_source
            self.usage_unknown = False

    def enrich_latest_usage_cost(self, cost_micros: int) -> None:
        """Attach a server-rate-derived cost to the latest receipt once."""

        try:
            amount = max(0, int(cost_micros))
        except (TypeError, ValueError, OverflowError):
            return
        with self._budget_lock:
            if not self.usage_records:
                return
            key = next(reversed(self.usage_records))
            record = self.usage_records[key]
            if record.get("cost_micros") is not None:
                return
            record["cost_micros"] = amount
            self.usage["cost_micros"] = self.usage.get("cost_micros", 0) + amount


_ACTIVE_TURN_LEDGER: ContextVar[UnifiedTurnLedger | None] = ContextVar(
    "unified_turn_ledger",
    default=None,
)


@contextmanager
def activate_unified_turn_ledger(
    ledger: UnifiedTurnLedger,
) -> Iterator[UnifiedTurnLedger]:
    """Expose an explicit ledger to wrappers that cannot forward new kwargs."""

    token = _ACTIVE_TURN_LEDGER.set(ledger)
    try:
        yield ledger
    finally:
        _ACTIVE_TURN_LEDGER.reset(token)


class RegistryToolRouter:
    """Execute unified calls through the existing ToolRegistry."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        log_prefix: str = "UnifiedTurn",
        config: Any | None = None,
        client: Any | None = None,
        user_input: str | None = None,
        enforce_tool_policy: bool = True,
        failure_breaker: ToolFailureCircuitBreaker | None = None,
        no_progress_guard: ToolNoProgressGuard | None = None,
    ):
        self.registry = registry
        self.log_prefix = log_prefix
        self.config = config
        # Optional provider reference lets request-time policy follow a
        # client's replaced Config object, not only mutations to the original
        # mapping captured by a persistent registry.
        self.client = client
        self.user_input = user_input
        self.enforce_tool_policy = enforce_tool_policy
        # The breaker is request/turn scoped.  Only schema-v3 Agent Team delegation
        # turns opt in by default; ordinary chat keeps its historical retry
        # behavior unless a caller explicitly supplies a breaker.
        if failure_breaker is not None:
            self.failure_breaker = failure_breaker
        elif (
            config is not None
            and agent_team_v3_delegation_enabled(config)
        ):
            self.failure_breaker = ToolFailureCircuitBreaker(
                max_same_failure=2,
                failed_tool_budget=8,
            )
        else:
            self.failure_breaker = None
        self.no_progress_guard = (
            no_progress_guard
            or current_tool_no_progress_guard()
            or ToolNoProgressGuard()
        )

    def _effective_config(self) -> Any | None:
        current = getattr(self.client, "config", None) if self.client is not None else None
        return current if current is not None else self.config

    def _tool_definition(self, call: UnifiedToolCall) -> Any | None:
        getter = getattr(self.registry, "get", None)
        if not callable(getter):
            return None
        try:
            return getter(call.tool)
        except Exception:  # noqa: BLE001 - compatibility fake registries
            return None

    def _observable_call(self, call: UnifiedToolCall) -> UnifiedToolCall:
        """Return the public/audit view of a call without hidden arguments."""

        observable_name = observable_tool_name(self.registry, call.tool)
        return UnifiedToolCall(
            tool=observable_name,
            arguments=_observable_tool_arguments(
                self.registry,
                call.tool,
                call.arguments,
            ),
            call_id=call.call_id,
        )

    def _observable_result(self, result: UnifiedToolResult) -> UnifiedToolResult:
        """Strip trusted wrapper arguments before a result leaves the router."""

        safe_call = self._observable_call(result.call)
        if safe_call == result.call:
            return result
        return UnifiedToolResult(
            call=safe_call,
            output=result.output,
            success=result.success,
            error=result.error,
            elapsed_ms=result.elapsed_ms,
            blocked_reason=result.blocked_reason,
            generation_failure=result.generation_failure,
            status_message=result.status_message,
            turn_terminal=result.turn_terminal,
            terminal_reason=result.terminal_reason,
            failure_group=result.failure_group,
            executed=result.executed,
            synthetic=result.synthetic,
        )

    @staticmethod
    def _framework_block_result(
        result: UnifiedToolResult,
        *,
        source: str,
    ) -> UnifiedToolResult:
        """Make a source-owned pre-execution block terminal and user-safe."""

        failure = tool_execution_blocked_failure(source=source)
        return replace(
            result,
            success=False,
            generation_failure=failure,
            status_message=failure.user_message,
            turn_terminal=True,
            terminal_reason=failure.kind,
            executed=False,
        )

    @staticmethod
    def _circuit_generation_failure(
        breaker: ToolFailureCircuitBreaker,
        family: str,
    ) -> GenerationFailure:
        """Project only a normalized breaker key into a terminal failure."""

        signature = ""
        try:
            opened = breaker.snapshot().get("opened", [])
        except Exception:
            opened = []
        if isinstance(opened, (list, tuple, set)):
            prefix = f"{family}:"
            signature = next(
                (
                    str(candidate).split("@", 1)[0]
                    for candidate in opened
                    if str(candidate).startswith(prefix)
                ),
                "",
            )
        return tool_failure_circuit_open_failure(
            family=family,
            signature=signature,
        )

    def _runtime_capability_block_result(
        self,
        call: UnifiedToolCall,
    ) -> UnifiedToolResult | None:
        """Revalidate stale registry capability state before policy/execute."""

        definition = self._tool_definition(call)
        if definition is None:
            return None
        try:
            from .tool_exposure import runtime_tool_capability_status

            reason = runtime_tool_capability_status(
                definition,
                client=self.client,
                config=self._effective_config(),
            )
        except Exception:
            # A supplied runtime config must never widen a stale optional
            # capability when the validator itself fails.  Config-less legacy
            # callers retain the registry's historical execution seam.
            reason = (
                "runtime capability could not be validated"
                if self._effective_config() is not None
                else None
            )
        if not reason:
            return None
        observable_name = observable_tool_name(self.registry, call.tool)
        output = f"Tool capability blocked `{observable_name}`: {reason}."
        logger.warning(
            "[%s] Runtime capability blocked %s: %s",
            self.log_prefix,
            observable_name,
            reason,
        )
        return self._framework_block_result(
            UnifiedToolResult(
                call=call,
                output=output,
                success=False,
                error=str(reason),
            ),
            source="runtime_capability",
        )

    def _agent_authority_block_result(
        self,
        call: UnifiedToolCall,
    ) -> UnifiedToolResult | None:
        """Revalidate the current Agent identity for sync tool lanes."""

        from ..services.turn_context import get_turn_context

        agent_execution_active = (
            getattr(get_turn_context(), "agent_execution_binding", None) is not None
        )
        try:
            from ..services.agent_authority_guard import guard_agent_tool_call_sync

            decision = guard_agent_tool_call_sync(
                call.tool,
                dict(call.arguments or {}),
                tool_definition=self._tool_definition(call),
                config=self._effective_config(),
            )
        except Exception as exc:
            logger.error(
                "[%s] Agent authority guard failed: exception_type=%s",
                self.log_prefix,
                type(exc).__name__,
            )
            decision = None
        if decision is None:
            if not agent_execution_active:
                return None
            observable_name = observable_tool_name(self.registry, call.tool)
            return self._framework_block_result(
                UnifiedToolResult(
                    call=call,
                    output=(
                        f"Tool execution blocked `{observable_name}`: "
                        "Agent authority could not be revalidated"
                    ),
                    success=False,
                    error="agent_authority_unavailable",
                ),
                source="agent_authority",
            )
        if decision.allowed:
            return None
        observable_name = observable_tool_name(self.registry, call.tool)
        return self._framework_block_result(
            UnifiedToolResult(
                call=call,
                output=f"Tool execution blocked `{observable_name}`: {decision.reason}",
                success=False,
                error="agent_authority_denied",
            ),
            source="agent_authority",
        )

    async def _agent_authority_block_result_async(
        self,
        call: UnifiedToolCall,
    ) -> UnifiedToolResult | None:
        """Revalidate the current Agent identity for async tool lanes."""

        from ..services.turn_context import get_turn_context

        agent_execution_active = (
            getattr(get_turn_context(), "agent_execution_binding", None) is not None
        )
        try:
            from ..services.agent_authority_guard import guard_agent_tool_call_async

            decision = await guard_agent_tool_call_async(
                call.tool,
                dict(call.arguments or {}),
                tool_definition=self._tool_definition(call),
                config=self._effective_config(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "[%s] Agent authority guard failed: exception_type=%s",
                self.log_prefix,
                type(exc).__name__,
            )
            decision = None
        if decision is None:
            if not agent_execution_active:
                return None
            observable_name = observable_tool_name(self.registry, call.tool)
            return self._framework_block_result(
                UnifiedToolResult(
                    call=call,
                    output=(
                        f"Tool execution blocked `{observable_name}`: "
                        "Agent authority could not be revalidated"
                    ),
                    success=False,
                    error="agent_authority_unavailable",
                ),
                source="agent_authority",
            )
        if decision.allowed:
            return None
        observable_name = observable_tool_name(self.registry, call.tool)
        return self._framework_block_result(
            UnifiedToolResult(
                call=call,
                output=f"Tool execution blocked `{observable_name}`: {decision.reason}",
                success=False,
                error="agent_authority_denied",
            ),
            source="agent_authority",
        )

    def _breaker_block_result(self, call: UnifiedToolCall) -> UnifiedToolResult | None:
        breaker = self.failure_breaker
        if breaker is None:
            return None
        # Classify only the observable/registry-authoritative name.  A
        # provider-controlled unknown such as ``docs_fake ...`` must not
        # inherit the Docs breaker and poison a real Docs failure budget.
        family = tool_failure_family(observable_tool_name(self.registry, call.tool))
        if not breaker.is_open(family, tool_failure_scope(call.tool, call.arguments)):
            return None
        payload = {
            "success": False,
            "error_code": "circuit_open",
            "retryable": False,
            "error": f"Tool failure circuit is open for {family}; retry suppressed",
        }
        logger.warning(
            "[%s] Tool failure circuit open; suppressing %s (%s)",
            self.log_prefix,
            observable_tool_name(self.registry, call.tool),
            family,
        )
        generation_failure = self._circuit_generation_failure(breaker, family)
        return UnifiedToolResult(
            call=call,
            output=json.dumps(payload, ensure_ascii=False),
            success=False,
            error=payload["error"],
            generation_failure=generation_failure,
            status_message=generation_failure.user_message,
            executed=False,
        )

    def _no_progress_validation_failure(
        self,
        call: UnifiedToolCall,
        parsed: Mapping[str, Any] | None,
        failure_group: str = "",
    ) -> GenerationFailure | None:
        if not isinstance(parsed, Mapping):
            return None
        error_code = str(parsed.get("error_code") or "").strip().lower()
        if (
            error_code not in _NO_PROGRESS_VALIDATION_CODES
            and not failure_group.startswith("correctable_once")
            and not _is_correctable_tool_failure(error_code)
        ):
            return None
        if self.no_progress_guard is None:
            self.no_progress_guard = ToolNoProgressGuard()
        guard = self.no_progress_guard
        return guard.observe_failure(
            tool_name=call.tool,
            arguments=call.arguments,
            error_code=error_code,
            failure_group=failure_group or None,
        )

    def _apply_failure_breaker(
        self,
        call: UnifiedToolCall,
        result: UnifiedToolResult,
    ) -> UnifiedToolResult:
        raw_tool_name = call.tool
        result = self._observable_result(result)
        call = result.call
        breaker = self.failure_breaker
        if result.success:
            return result
        if self.no_progress_guard is None and result.failure_group.startswith(
            "correctable_once"
        ):
            # Typed planning correction budgets are request-local even when
            # the legacy failure breaker is disabled for this router.
            self.no_progress_guard = ToolNoProgressGuard()
        parsed = parse_structured_tool_failure(result.output)
        if parsed is None:
            lowered = str(result.output or result.error or "").strip().lower()
            legacy_code = (
                "not_found"
                if lowered.startswith(("tool not found", "not found:"))
                else "ambiguous_target"
                if "ambiguous" in lowered
                else "validation"
                if "validation" in lowered or "invalid" in lowered
                else "tool_error"
            )
            failure = {
                "success": False,
                "error_code": legacy_code,
                "retryable": False,
                "error": result.error or result.output,
            }
        else:
            # ``parse_structured_tool_failure`` returns the normalized
            # diagnostic fields, while ``ToolFailureCircuitBreaker.check``
            # intentionally accepts only an explicitly failed envelope. Keep
            # that marker when handing the normalized view back to the
            # breaker; otherwise the breaker discards the safe error code and
            # falls back to ``tool_error``.
            failure = {"success": False, **parsed}
            if _is_command_tool_name(raw_tool_name) or _is_command_tool_name(call.tool):
                try:
                    command_payload = json.loads(result.output)
                except (TypeError, ValueError, json.JSONDecodeError):
                    command_payload = None
                if _command_nonzero_failure(command_payload):
                    failure = {
                        "success": False,
                        **parsed,
                        "error_code": "command_failed",
                        "diagnostic_code": "command_nonzero_exit",
                        "root_cause": "command_nonzero_exit",
                    }
                    if self.no_progress_guard is None:
                        self.no_progress_guard = ToolNoProgressGuard()
                    repeated_exit = self.no_progress_guard.observe_command_exit(
                        tool_name=call.tool,
                        arguments=call.arguments,
                        output=result.output,
                    )
                    if repeated_exit is not None:
                        return replace(
                            result,
                            generation_failure=repeated_exit,
                            status_message=repeated_exit.user_message,
                        )
                elif not is_internal_tool_failure_code(parsed.get("error_code")):
                    # Scope denials, timeouts and malformed wrappers are not
                    # model-correctable command exits; keep them fail-closed.
                    failure = {
                        "success": False,
                        **parsed,
                        "error_code": "command_unclassified_failure",
                        "root_cause": "command_unclassified_failure",
                    }
        terminal_validation_failure = self._no_progress_validation_failure(
            call,
            failure,
            result.failure_group,
        )
        if terminal_validation_failure is not None:
            # The call did run: keep its real output/error/executed so the
            # receipt shows the actual cause.  The stop decision travels in
            # ``generation_failure``/``status_message`` (like Hermes'
            # separate outcome vs. control), not by replacing the result.
            return replace(
                result,
                generation_failure=terminal_validation_failure,
                status_message=terminal_validation_failure.user_message,
            )

        if breaker is None:
            return result

        decision = breaker.check(
            tool_failure_family(observable_tool_name(self.registry, call.tool)),
            failure,
            scope=tool_failure_scope(call.tool, call.arguments),
        )
        if decision.allowed:
            return result
        signature = (
            decision.signature.key
            if decision.signature
            else tool_failure_family(observable_tool_name(self.registry, call.tool))
        )
        logger.warning(
            "[%s] Tool failure circuit opened for %s: %s",
            self.log_prefix,
            observable_tool_name(self.registry, call.tool),
            signature,
        )
        generation_failure = tool_failure_circuit_open_failure(
            family=tool_failure_family(
                observable_tool_name(self.registry, call.tool)
            ),
            signature=signature,
        )
        # Keep the executed result (output/error/executed/synthetic) intact;
        # only the control decision is attached.
        return replace(
            result,
            generation_failure=generation_failure,
            status_message=generation_failure.user_message,
        )

    def _policy_block_result(self, call: UnifiedToolCall) -> UnifiedToolResult | None:
        if not self.enforce_tool_policy:
            return None
        effective_config = self._effective_config()
        if effective_config is None and call.tool == "search_past_chats":
            return None
        try:
            from .tool_policy import (
                check_tool_call_allowed,
                format_blocked_tool_result,
                get_current_user_input,
            )

            decision = check_tool_call_allowed(
                call.tool,
                user_input=self.user_input or get_current_user_input(),
                tool_args=dict(call.arguments or {}),
                config=effective_config,
                tool_definition=self._tool_definition(call),
            )
        except Exception as exc:
            logger.error(
                "[%s] Tool policy check failed: exception_type=%s",
                self.log_prefix,
                type(exc).__name__,
            )
            return UnifiedToolResult(
                call=call,
                output=(
                    "Tool execution was blocked because the deterministic "
                    "tool policy could not be evaluated."
                ),
                success=False,
                error="tool policy unavailable",
                blocked_reason="tool policy unavailable",
                executed=False,
            )

        if decision.allowed:
            return None

        observable_name = observable_tool_name(self.registry, call.tool)
        output = format_blocked_tool_result(observable_name, decision)
        logger.warning(
            "[%s] Tool policy blocked %s: %s",
            self.log_prefix,
            observable_name,
            decision.reason,
        )
        blocked = UnifiedToolResult(
            call=call,
            output=output,
            success=False,
            error="",
            blocked_reason=str(decision.reason or "tool policy blocked"),
            # A policy refusal never ran the tool.
            executed=False,
        )
        from .tool_policy import (
            current_structured_task_mode,
            record_structured_task_policy_denial,
            tool_definition_is_mutating,
        )

        if current_structured_task_mode() is not None and tool_definition_is_mutating(
            call.tool,
            tool_definition=self._tool_definition(call),
        ):
            # A structured Agent task must not route around a write denial
            # with another write tool or a review-driven retry.  The denial
            # is terminal for the turn and recorded for task settlement.
            record_structured_task_policy_denial(blocked.blocked_reason)
            return self._framework_block_result(blocked, source="tool_policy")
        return blocked

    def _approval_block_result(self, call: UnifiedToolCall) -> UnifiedToolResult | None:
        registry_get = getattr(self.registry, "get", None)
        if not callable(registry_get):
            return None
        tool_def = registry_get(call.tool)
        if tool_def is None or not getattr(tool_def, "requires_approval", False):
            return None
        try:
            from ..tools.external_llm_permission import check_permission_sync

            approved = check_permission_sync(
                call.tool,
                dict(call.arguments or {}),
                f"Run tool `{call.tool}`",
            )
        except Exception as exc:
            logger.error(
                "[%s] Tool approval check failed: tool=%s exception_type=%s",
                self.log_prefix,
                observable_tool_name(self.registry, call.tool),
                type(exc).__name__,
            )
            approved = False
        if approved:
            return None
        observable_name = observable_tool_name(self.registry, call.tool)
        return UnifiedToolResult(
            call=call,
            output=f"Tool permission denied: `{observable_name}` was not approved.",
            success=False,
            error="",
        )

    async def _approval_block_result_async(
        self,
        call: UnifiedToolCall,
    ) -> UnifiedToolResult | None:
        registry_get = getattr(self.registry, "get", None)
        if not callable(registry_get):
            return None
        tool_def = registry_get(call.tool)
        if tool_def is None or not getattr(tool_def, "requires_approval", False):
            return None
        try:
            from ..tools.external_llm_permission import check_permission

            approved = await check_permission(
                call.tool,
                dict(call.arguments or {}),
                f"Run tool `{call.tool}`",
            )
        except Exception as exc:
            logger.error(
                "[%s] Tool approval check failed: tool=%s exception_type=%s",
                self.log_prefix,
                observable_tool_name(self.registry, call.tool),
                type(exc).__name__,
            )
            approved = False
        if approved:
            return None
        observable_name = observable_tool_name(self.registry, call.tool)
        return UnifiedToolResult(
            call=call,
            output=f"Tool permission denied: `{observable_name}` was not approved.",
            success=False,
            error="",
        )

    def _policy_denied_result(
        self,
        call: UnifiedToolCall,
        exc: ToolPolicyDeniedError,
        *,
        elapsed_ms: float = 0.0,
    ) -> UnifiedToolResult:
        """Record a tool-level policy refusal as not executed, keeping its reason."""

        payload = {
            "success": False,
            "error_code": "tool_policy_denied",
            "retryable": False,
            "error": f"Blocked by task execution policy: {exc.reason}",
        }
        return UnifiedToolResult(
            call=self._observable_call(call),
            output=json.dumps(payload, ensure_ascii=False),
            success=False,
            error=payload["error"],
            elapsed_ms=elapsed_ms,
            executed=False,
            policy_denial={"code": exc.code},
        )

    def _argument_validation_result(
        self,
        call: UnifiedToolCall,
        exc: ToolArgumentValidationError,
        *,
        elapsed_ms: float = 0.0,
    ) -> UnifiedToolResult:
        payload = {
            "success": False,
            "error_code": exc.code,
            "retryable": False,
            "error": "Tool arguments were rejected by the server contract.",
        }
        logger.warning(
            "[%s] Tool argument validation rejected %s: code=%s",
            self.log_prefix,
            observable_tool_name(self.registry, call.tool),
            exc.code,
        )
        # Do not retain the rejected provider payload in the result object:
        # callers may persist ``tool_results`` or forward them to callbacks.
        safe_call = UnifiedToolCall(
            tool=observable_tool_name(self.registry, call.tool),
            arguments={},
            call_id=call.call_id,
        )
        result = UnifiedToolResult(
            call=safe_call,
            output=json.dumps(payload, ensure_ascii=False),
            success=False,
            error=payload["error"],
            elapsed_ms=elapsed_ms,
            executed=False,
            argument_rejection={
                "code": exc.code,
                **argument_correlation(call.arguments, call.tool),
            },
        )
        if self.no_progress_guard is not None:
            terminal_failure = self.no_progress_guard.observe_failure(
                tool_name=safe_call.tool,
                # Keep raw values only inside the request-local hash builder;
                # the returned/audited call remains the redacted safe_call.
                arguments=call.arguments,
                error_code=exc.code,
            )
            if terminal_failure is not None:
                terminal_payload = {
                    "success": False,
                    "error_code": "circuit_open",
                    "retryable": False,
                    "error": terminal_failure.user_message,
                    "retry_suppressed": True,
                }
                return UnifiedToolResult(
                    call=safe_call,
                    output=json.dumps(terminal_payload, ensure_ascii=False),
                    success=False,
                    error=terminal_failure.user_message,
                    elapsed_ms=elapsed_ms,
                    generation_failure=terminal_failure,
                    status_message=terminal_failure.user_message,
                    executed=False,
                )
        # Contract validation failures get one concrete correction opportunity;
        # the request-local guard above only stops an unchanged repeat.
        return result

    def _normalize_call_arguments(
        self,
        call: UnifiedToolCall,
    ) -> tuple[UnifiedToolCall, UnifiedToolResult | None]:
        """Canonicalize arguments before policy, approval, or Python execution."""

        definition = self._tool_definition(call)
        if definition is None:
            if _registry_contains(self.registry, call.tool):
                # Legacy registry adapters can execute a trusted name without
                # exposing a ToolDefinition/schema. Keep their historical
                # argument forwarding contract; observation remains empty.
                return call, None
            # Unknown tool names still produce a structured router failure,
            # but their provider payload must not be retained in results or
            # audit records where it could expose arbitrary fields.
            return (
                UnifiedToolCall(
                    # Keep the provider name authoritative for the internal
                    # registry lookup. Public/audit projections call
                    # ``observable_tool_name`` and remain bounded.
                    tool=call.tool,
                    arguments={},
                    call_id=call.call_id,
                ),
                None,
            )
        normalizer = getattr(definition, "normalize_arguments", None)
        if not callable(normalizer):
            # Compatibility fake definitions keep their legacy behavior.
            return call, None
        try:
            normalized = normalizer(call.arguments)
        except ToolArgumentValidationError as exc:
            return call, self._argument_validation_result(call, exc)
        except Exception as exc:
            # A repository-controlled schema/normalizer defect must fail
            # closed without serializing exception text or argument values.
            logger.error(
                "[%s] Tool argument normalizer failed for %s: exception_type=%s",
                self.log_prefix,
                observable_tool_name(self.registry, call.tool),
                type(exc).__name__,
            )
            failure = ToolArgumentValidationError(
                call.tool,
                code="tool_argument_invalid",
            )
            return call, self._argument_validation_result(call, failure)

        return (
            UnifiedToolCall(
                tool=call.tool,
                arguments=dict(normalized),
                call_id=call.call_id,
                raw=call.raw,
            ),
            None,
        )

    def execute(self, call: UnifiedToolCall) -> UnifiedToolResult:
        call, invalid = self._normalize_call_arguments(call)
        if invalid is not None:
            return invalid
        blocked = self._breaker_block_result(call)
        if blocked is not None:
            return self._observable_result(blocked)
        blocked = self._runtime_capability_block_result(call)
        if blocked is not None:
            return self._observable_result(blocked)
        blocked = self._agent_authority_block_result(call)
        if blocked is not None:
            return self._observable_result(blocked)
        blocked = self._policy_block_result(call)
        if blocked is not None:
            return self._observable_result(blocked)
        blocked = self._approval_block_result(call)
        if blocked is not None:
            return self._observable_result(blocked)

        started = time.perf_counter()
        status_message = ""
        turn_terminal = False
        terminal_reason = ""
        failure_group = ""
        try:
            from ..services.turn_context import (
                reset_turn_context,
                set_turn_tool_call_id,
            )

            tool_context_token = set_turn_tool_call_id(call.call_id)
            try:
                result = self.registry.execute(call.tool, **dict(call.arguments or {}))
            finally:
                reset_turn_context(tool_context_token)
            planning_projection = _planning_tool_outcome_projection(result)
            if planning_projection is not None:
                output = str(planning_projection["output"])
                success = False
                error = str(planning_projection["error"])
                status_message = str(planning_projection["status_message"])
                turn_terminal = bool(planning_projection["turn_terminal"])
                terminal_reason = str(planning_projection["terminal_reason"])
                failure_group = str(planning_projection["failure_group"])
            else:
                output = _tool_result_output(call.tool, result)
                success, error = tool_output_indicates_success(result)
        except (GenerationInterrupted, PlanningInteractionTerminated):
            # Generation control flow is not a tool failure.  Let the owning
            # response/terminal layer handle steering or terminal planning
            # instead of serializing the exception as a model-visible result.
            raise
        except ToolArgumentValidationError as exc:
            return self._argument_validation_result(
                call,
                exc,
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )
        except ToolPolicyDeniedError as exc:
            return self._policy_denied_result(
                call,
                exc,
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )
        except Exception as exc:
            # Provider/tool exception text may contain credentials, URLs, or
            # document content.  Keep the result machine-readable and let the
            # failure breaker classify it without echoing raw text to logs.
            safe_code = _safe_exception_code(exc)
            output = (
                "Tool not found: unavailable"
                if self._tool_definition(call) is None
                else "Tool execution failed."
                if not safe_code
                else f"Tool execution failed. (reason: {safe_code})"
            )
            success = False
            error = output
            logger.error(
                "[%s] Tool execution failed: tool=%s exception_type=%s reason=%s",
                self.log_prefix,
                observable_tool_name(self.registry, call.tool),
                type(exc).__name__,
                safe_code or "-",
            )

        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "[%s] Tool %s completed: success=%s elapsed_ms=%.1f",
            self.log_prefix,
            observable_tool_name(self.registry, call.tool),
            success,
            elapsed_ms,
        )
        result = UnifiedToolResult(
            call=self._observable_call(call),
            output=output,
            success=success,
            error=error,
            elapsed_ms=elapsed_ms,
            status_message=status_message,
            turn_terminal=turn_terminal,
            terminal_reason=terminal_reason,
            failure_group=failure_group,
        )
        if success and self.no_progress_guard is not None:
            if _is_file_mutation_tool_name(call.tool):
                self.no_progress_guard.note_effect()
            self.no_progress_guard.record_progress(
                tool_name=call.tool,
                arguments=call.arguments,
                output=result.output,
            )
        if result.turn_terminal:
            return result
        return self._apply_failure_breaker(call, result)

    async def execute_async(self, call: UnifiedToolCall) -> UnifiedToolResult:
        call, invalid = self._normalize_call_arguments(call)
        if invalid is not None:
            return invalid
        blocked = self._breaker_block_result(call)
        if blocked is not None:
            return self._observable_result(blocked)
        blocked = self._runtime_capability_block_result(call)
        if blocked is not None:
            return self._observable_result(blocked)
        blocked = await self._agent_authority_block_result_async(call)
        if blocked is not None:
            return self._observable_result(blocked)
        blocked = self._policy_block_result(call)
        if blocked is not None:
            return self._observable_result(blocked)
        blocked = await self._approval_block_result_async(call)
        if blocked is not None:
            return self._observable_result(blocked)

        started = time.perf_counter()
        status_message = ""
        turn_terminal = False
        terminal_reason = ""
        failure_group = ""
        try:
            from ..services.turn_context import (
                reset_turn_context,
                set_turn_tool_call_id,
            )

            tool_context_token = set_turn_tool_call_id(call.call_id)
            try:
                result = await self.registry.execute_async(
                    call.tool,
                    **dict(call.arguments or {}),
                )
            finally:
                reset_turn_context(tool_context_token)
            planning_projection = _planning_tool_outcome_projection(result)
            if planning_projection is not None:
                output = str(planning_projection["output"])
                success = False
                error = str(planning_projection["error"])
                status_message = str(planning_projection["status_message"])
                turn_terminal = bool(planning_projection["turn_terminal"])
                terminal_reason = str(planning_projection["terminal_reason"])
                failure_group = str(planning_projection["failure_group"])
            else:
                output = _tool_result_output(call.tool, result)
                success, error = tool_output_indicates_success(result)
        except (GenerationInterrupted, PlanningInteractionTerminated):
            # Keep generation control flow out of model-visible tool errors.
            raise
        except ToolArgumentValidationError as exc:
            return self._argument_validation_result(
                call,
                exc,
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )
        except ToolPolicyDeniedError as exc:
            return self._policy_denied_result(
                call,
                exc,
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )
        except Exception as exc:
            # Do not write raw provider/tool exception text into logs or the
            # model-facing result object.
            safe_code = _safe_exception_code(exc)
            output = (
                "Tool not found: unavailable"
                if self._tool_definition(call) is None
                else "Tool execution failed."
                if not safe_code
                else f"Tool execution failed. (reason: {safe_code})"
            )
            success = False
            error = output
            logger.error(
                "[%s] Tool execution failed: tool=%s exception_type=%s reason=%s",
                self.log_prefix,
                observable_tool_name(self.registry, call.tool),
                type(exc).__name__,
                safe_code or "-",
            )

        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "[%s] Tool %s completed: success=%s elapsed_ms=%.1f",
            self.log_prefix,
            observable_tool_name(self.registry, call.tool),
            success,
            elapsed_ms,
        )
        result = UnifiedToolResult(
            call=self._observable_call(call),
            output=output,
            success=success,
            error=error,
            elapsed_ms=elapsed_ms,
            status_message=status_message,
            turn_terminal=turn_terminal,
            terminal_reason=terminal_reason,
            failure_group=failure_group,
        )
        if success and self.no_progress_guard is not None:
            if _is_file_mutation_tool_name(call.tool):
                self.no_progress_guard.note_effect()
            self.no_progress_guard.record_progress(
                tool_name=call.tool,
                arguments=call.arguments,
                output=result.output,
            )
        if result.turn_terminal:
            return result
        return self._apply_failure_breaker(call, result)

    def execute_many(self, calls: Sequence[UnifiedToolCall]) -> list[UnifiedToolResult]:
        # Keep execution ordered for compatibility.  ToolDefinition already
        # carries supports_parallel; a later pass can safely batch independent
        # calls here without touching provider adapters.
        return [self.execute(call) for call in calls]


def clip_text(text: str, max_chars: int | None) -> str:
    if max_chars is None or max_chars <= 0:
        return text
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}... [truncated]"


def openai_message_content(
    message: Any,
    extractor: Callable[[Any], str] | None = None,
) -> str:
    if extractor is not None:
        return str(extractor(message) or "")
    return str(getattr(message, "content", None) or "")


class ToolCallArgumentsError(ValueError):
    """Provider tool arguments are unusable and must not be invoked.

    The rendered message is safe to log or persist verbatim: it carries a short
    reason code and the tool name only. Raw argument text is never embedded,
    because provider arguments may contain credentials or other secrets.
    """

    def __init__(self, reason: str, *, tool_name: str = "") -> None:
        self.reason = str(reason)
        self.tool_name = str(tool_name or "")
        super().__init__(
            "invalid tool call arguments"
            f" (tool={self.tool_name or '<unknown>'}, reason={self.reason})"
        )


def parse_tool_call_arguments(
    raw_args: Any,
    *,
    tool_name: str = "",
    present: bool = True,
) -> dict[str, Any]:
    """Parse one provider ``arguments`` payload strictly (fail closed).

    Before Phase 2 every unparsable / non-object payload collapsed to ``{}``,
    which turned a malformed call into an authorized default no-argument
    invocation. Only an explicit JSON object — including the explicit ``{}``
    form — may reach authorization and execution.

    Missing-argument support is deliberately not invented here: a provider that
    omits ``arguments`` has no contract evidence for a no-argument call, so it
    is rejected like any other unusable payload.
    """

    if isinstance(raw_args, dict):
        return dict(raw_args)
    if not present:
        raise ToolCallArgumentsError("missing_arguments", tool_name=tool_name)
    if raw_args is None:
        raise ToolCallArgumentsError("null_arguments", tool_name=tool_name)
    if not isinstance(raw_args, str):
        raise ToolCallArgumentsError(
            f"non_text_arguments:{type(raw_args).__name__}",
            tool_name=tool_name,
        )
    text = raw_args.strip()
    if not text:
        raise ToolCallArgumentsError("empty_arguments", tool_name=tool_name)
    try:
        parsed = json.loads(text)
    except Exception as exc:  # noqa: BLE001 - reason code only, never the payload
        raise ToolCallArgumentsError(
            f"unparsable_json:{type(exc).__name__}",
            tool_name=tool_name,
        ) from None
    if not isinstance(parsed, dict):
        raise ToolCallArgumentsError(
            f"non_object_json:{type(parsed).__name__}",
            tool_name=tool_name,
        )
    return parsed


def openai_tool_call_function(tool_call: Any) -> tuple[str, dict[str, Any]]:
    function = getattr(tool_call, "function", None)
    if function is None and isinstance(tool_call, dict):
        function = tool_call.get("function")

    if isinstance(function, dict):
        raw_name = function.get("name")
        name = str(raw_name or "")
        present = "arguments" in function
        raw_args = function.get("arguments")
    else:
        raw_name = getattr(function, "name", None)
        name = str(raw_name or "")
        present = hasattr(function, "arguments")
        raw_args = getattr(function, "arguments", None)

    # A provider call without a usable tool name is malformed output; reject it
    # before any authority or registry handling (reason code only, no payload).
    if function is None:
        raise ToolCallArgumentsError("missing_function")
    if not isinstance(raw_name, str):
        raise ToolCallArgumentsError("non_text_tool_name")
    if not raw_name.strip():
        raise ToolCallArgumentsError("missing_tool_name")

    return name, parse_tool_call_arguments(
        raw_args,
        tool_name=name,
        present=present,
    )


def _openai_tool_call_function_or_error(
    tool_call: Any,
) -> tuple[str, dict[str, Any], str]:
    """Return ``(name, arguments, error_reason)`` without raising.

    A non-empty ``error_reason`` means the arguments were unusable; the
    arguments are then ``{}`` for display only and the call must never reach
    authorization or execution.
    """

    try:
        name, arguments = openai_tool_call_function(tool_call)
    except ToolCallArgumentsError as exc:
        return exc.tool_name, {}, exc.reason
    return name, arguments, ""


def _raw_intent_digest(arguments: Any) -> str:
    from ..services.agent_run_service import canonical_audit_digest

    return canonical_audit_digest(arguments if isinstance(arguments, dict) else {})


def _tool_call_intent_fingerprint(tool_name: str, arguments: dict[str, Any]) -> str:
    """Canonical, order-insensitive intent used only for in-memory comparison."""

    return json.dumps(
        [str(tool_name), arguments],
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )


def openai_tool_call_id(tool_call: Any) -> str:
    if isinstance(tool_call, dict):
        return str(tool_call.get("id") or "")
    return str(getattr(tool_call, "id", "") or "")


def serialize_openai_tool_call(tool_call: Any) -> dict[str, Any]:
    if isinstance(tool_call, dict):
        serialized = dict(tool_call)
        function = serialized.get("function")
        if isinstance(function, dict):
            normalized_function = dict(function)
            arguments = normalized_function.get("arguments", "{}")
            if isinstance(arguments, dict):
                normalized_function["arguments"] = json.dumps(
                    arguments,
                    ensure_ascii=False,
                )
            serialized["function"] = normalized_function
        return serialized

    function = getattr(tool_call, "function", None)
    raw_arguments = getattr(function, "arguments", "{}")
    if isinstance(raw_arguments, dict):
        raw_arguments = json.dumps(raw_arguments, ensure_ascii=False)
    return {
        "id": getattr(tool_call, "id", ""),
        "type": getattr(tool_call, "type", "function"),
        "function": {
            "name": getattr(function, "name", ""),
            "arguments": raw_arguments,
        },
    }


def serialize_openai_assistant_message(
    message: Any,
    *,
    registry: ToolRegistry | None = None,
) -> dict[str, Any]:
    if isinstance(message, dict):
        payload = dict(message)
    else:
        dumped = None
        model_dump = getattr(message, "model_dump", None)
        if callable(model_dump):
            try:
                dumped = model_dump(exclude_none=True)
            except TypeError:
                dumped = model_dump()
        payload = dict(dumped) if isinstance(dumped, dict) else {}
        extra = getattr(message, "model_extra", None)
        if isinstance(extra, dict):
            payload.update(extra)
        for field_name in (
            "reasoning_content",
            "reasoning",
            "thinking_content",
            "thinking",
        ):
            if field_name not in payload:
                value = getattr(message, field_name, None)
                if value is not None:
                    payload[field_name] = value
    payload["role"] = str(payload.get("role") or getattr(message, "role", "assistant") or "assistant")
    payload["content"] = payload.get("content") or getattr(message, "content", "") or ""
    calls = list(payload.get("tool_calls") or getattr(message, "tool_calls", None) or [])
    if calls:
        serialized_calls = [serialize_openai_tool_call(call) for call in calls]
        if registry is not None:
            for serialized in serialized_calls:
                function = serialized.get("function")
                if not isinstance(function, dict):
                    continue
                provider_name = str(function.get("name") or "")
                observable_name = observable_tool_name(registry, provider_name)
                raw_arguments = function.get("arguments", "{}")
                if isinstance(raw_arguments, dict):
                    parsed_arguments = raw_arguments
                else:
                    try:
                        parsed_arguments = json.loads(raw_arguments or "{}")
                    except Exception:
                        parsed_arguments = {}
                function["name"] = observable_name
                function["arguments"] = json.dumps(
                    _observable_tool_arguments(
                        registry,
                        provider_name,
                        parsed_arguments if isinstance(parsed_arguments, dict) else {},
                    ),
                    ensure_ascii=False,
                )
        payload["tool_calls"] = serialized_calls
    return payload


def openai_tool_calls_from_message(message: Any) -> list[UnifiedToolCall]:
    calls = []
    for raw_call in getattr(message, "tool_calls", None) or []:
        name, arguments = openai_tool_call_function(raw_call)
        calls.append(
            UnifiedToolCall(
                tool=name,
                arguments=arguments,
                call_id=openai_tool_call_id(raw_call),
                raw=raw_call,
            )
        )
    return calls


def cli_tool_calls_from_parsed(tool_calls: Sequence[dict[str, Any]]) -> list[UnifiedToolCall]:
    calls: list[UnifiedToolCall] = []
    for index, tool_call in enumerate(tool_calls):
        name = str(tool_call.get("name") or "")
        args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
        calls.append(
            UnifiedToolCall(
                tool=name,
                arguments=dict(args or {}),
                call_id=str(tool_call.get("id") or f"cli-call-{index + 1}"),
                raw=tool_call,
            )
        )
    return calls


def _cli_config_get(config: Any | None, key: str, default: Any = None) -> Any:
    if isinstance(config, dict):
        value: Any = config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value
    getter = getattr(config, "get", None)
    if callable(getter):
        return getter(key, default)
    return default


def _cli_tool_context_max_chars(config: Any | None) -> int:
    configured = _cli_config_get(
        config,
        "llm_cli.tool_result_context_max_chars",
        None,
    )
    try:
        value = (
            int(configured)
            if configured is not None
            else DEFAULT_CLI_TOOL_CONTEXT_MAX_CHARS
        )
    except (TypeError, ValueError):
        value = DEFAULT_CLI_TOOL_CONTEXT_MAX_CHARS
    # Neither setting may disable the aggregate safety cap.  Keep enough room
    # for at least one compact result plus the completion-control text.
    return max(4_000, min(value, MAX_CLI_TOOL_CONTEXT_MAX_CHARS))


def format_cli_tool_results(
    results: Sequence[UnifiedToolResult],
    *,
    max_chars: int | None = None,
    config: Any | None = None,
    user_input: str = "",
) -> str:
    lines = ["Tool results:"]
    for result in results:
        source = result.output if result.success else result.error or result.output
        payload = model_tool_result_payload(
            tool_name=result.call.tool,
            output=source,
            user_input=user_input,
            max_chars=max_chars,
            config=config,
            legacy_clip=clip_text,
        )
        if result.success:
            lines.append(f"  [{result.call.tool}] {payload.text}")
        else:
            lines.append(f"  [{result.call.tool}] Error: {payload.text}")
    return "\n".join(lines)


def format_cli_tool_history(
    results: Sequence[UnifiedToolResult],
    *,
    original_input: str,
    max_chars: int | None = None,
    max_total_chars: int | None = None,
) -> str:
    """Summarize same-turn CLI tool history for follow-up completion control."""

    results_list = list(results)
    if max_total_chars is not None and max_total_chars > 0:
        unbounded = format_cli_tool_history(
            results_list,
            original_input=original_input,
            max_chars=max_chars,
        )
        if len(unbounded) > max_total_chars:
            # Keep the newest successful/failed operations first.  The
            # current round is supplied separately, so older history is the
            # safest context to discard when the aggregate budget is tight.
            selected = ""
            for count in range(1, len(results_list) + 1):
                candidate = format_cli_tool_history(
                    results_list[-count:],
                    original_input=original_input,
                    max_chars=max_chars,
                )
                if len(candidate) <= max_total_chars:
                    selected = candidate
                    break
            if selected:
                return selected
            # Even one result may exceed a very small budget.  Clip the
            # newest result rather than the oldest prefix of the full history
            # so the follow-up retains the most actionable context.
            newest = format_cli_tool_history(
                results_list[-1:],
                original_input=original_input,
                max_chars=max_chars,
            )
            return newest[:max_total_chars]
        return unbounded

    lines = [
        "Same-turn tool history:",
        f"  Original objective: {original_input}",
    ]
    if not results_list:
        lines.extend(
            [
                "  Already executed tools: none",
                "  Successful tool results: none",
                "  Failed tool results: none",
                "  Remaining original objective: decide whether a tool is required.",
            ]
        )
        return "\n".join(lines)

    executed = [
        f"{index}. {result.call.tool}({json.dumps(result.call.arguments, ensure_ascii=False, sort_keys=True)})"
        for index, result in enumerate(results_list, start=1)
    ]
    successful = []
    failed = []
    for index, result in enumerate(results_list, start=1):
        output = clip_text(result.output, max_chars)
        error = clip_text(result.error or result.output, max_chars)
        entry = f"{index}. [{result.call.tool}] {output}"
        if result.success:
            successful.append(entry)
        else:
            failed.append(f"{index}. [{result.call.tool}] Error: {error}")

    lines.append("  Already executed tools:")
    lines.extend(f"    {entry}" for entry in executed)
    lines.append("  Successful tool results:")
    lines.extend(f"    {entry}" for entry in successful or ["none"])
    lines.append("  Failed tool results:")
    lines.extend(f"    {entry}" for entry in failed or ["none"])
    lines.append(
        "  Remaining original objective: first decide whether the original user request is already satisfied by the successful results above."
    )
    return "\n".join(lines)


def format_cli_follow_up_tool_context(
    current_results: Sequence[UnifiedToolResult],
    all_results: Sequence[UnifiedToolResult],
    *,
    original_input: str,
    max_chars: int | None = None,
    config: Any | None = None,
    user_input: str = "",
) -> str:
    context_limit = _cli_tool_context_max_chars(config)
    current_text = format_cli_tool_results(
        current_results,
        max_chars=max_chars,
        config=config,
        user_input=user_input or original_input,
    )
    completion_text = CLI_TOOL_COMPLETION_CONTROL_PROMPT
    separator_chars = 4  # two ``\n\n`` separators
    current_budget = max(1_000, context_limit - len(completion_text) - separator_chars)
    if len(current_text) > current_budget:
        current_text = clip_text(current_text, current_budget)
    history_budget = max(
        1_000,
        context_limit - len(completion_text) - len(current_text) - separator_chars,
    )
    history_text = format_cli_tool_history(
        all_results,
        original_input=original_input,
        max_chars=max_chars,
        max_total_chars=history_budget,
    )
    combined = "\n\n".join([current_text, history_text, completion_text])
    # Defensive final clamp for unusually long control/objective strings or
    # malformed user settings.  The recent current-round context is retained
    # ahead of older history by the budgeting above.
    if len(combined) > context_limit:
        # ``clip_text`` intentionally appends a human-readable suffix and can
        # therefore exceed its nominal limit.  Follow-up context is a hard
        # provider-input budget, so use a strict final clamp here.
        return combined[:context_limit]
    return combined


CLI_TOOL_COMPLETION_CONTROL_PROMPT = """Completion control after tool results:
- After receiving tool results, first decide whether the original user request is already satisfied.
- If satisfied, output the final answer immediately and do not call another tool.
- Call another tool only when the previous result is missing, failed, contradictory, stale, or the original request still has unfinished subgoals.
- A new tool call is also allowed when the previous result created a concrete new target to inspect, or when a mutation was made and post-change verification is still incomplete.
- Do not call another tool just to reconfirm a successful result.
- Re-running the same tool is allowed only when a concrete new reason exists, such as verifying a mutation, checking a changed file, resolving an error, or completing a distinct remaining subtask."""

SUPPRESSED_TOOL_FINAL_ANSWER_INSTRUCTION = (
    "The confirmed tool result above is authoritative. Do not call any tool again. "
    "Write the final answer now; if the evidence is insufficient, say so instead of guessing."
)

# A status query is a live observation. Reusing a successful suffix for an
# identical request can hide a worker transition and turn the final answer into
# a stale snapshot. The ordinary round budget still bounds repeated polling.
_NON_CACHEABLE_SUCCESSFUL_RECALL_TOOLS = frozenset(
    {"get_agent_work_status"}
)


def _cli_tool_call_signature(call: UnifiedToolCall) -> tuple[str, str]:
    return (
        call.tool,
        json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, default=str),
    )


def _terminal_skip_reason(reason: str) -> str:
    """Project a bounded framework reason into a synthetic receipt message."""

    normalized = str(reason or "").strip().lower()
    if normalized == "approval_already_pending":
        return "Not executed: approval is already pending."
    if normalized == "planning_not_active":
        return "Not executed: planning is not active."
    if normalized == "tool_failure_circuit_open":
        return "Not executed: tool failure circuit opened."
    return "Not executed: the framework stopped this turn."


def _terminal_skip_message(reason: str) -> str:
    normalized = str(reason or "").strip().lower()
    if normalized == "approval_already_pending":
        return "承認待ちのため未実行です"
    if normalized == "planning_not_active":
        return "計画が有効でないため未実行です"
    if normalized == "tool_failure_circuit_open":
        return "同じツール失敗の反復停止により未実行です"
    return "処理停止により未実行です"


def _synthetic_not_executed_result(
    router: RegistryToolRouter,
    call: UnifiedToolCall,
    *,
    reason: str,
) -> UnifiedToolResult:
    """Keep a provider batch transcript aligned after a terminal stop.

    The synthetic result is display/model context only.  Callers must keep it
    out of the execution ledger and provider usage audit.
    """

    safe_call = UnifiedToolCall(
        tool=observable_tool_name(router.registry, call.tool),
        arguments=_observable_tool_arguments(
            router.registry,
            call.tool,
            call.arguments,
        ),
        call_id=call.call_id,
    )
    payload = {
        "success": False,
        "error_code": "not_executed",
        "retryable": False,
        "error": reason,
    }
    rendered = json.dumps(payload, ensure_ascii=False)
    return UnifiedToolResult(
        call=safe_call,
        output=rendered,
        success=False,
        error=reason,
        status_message=_terminal_skip_message(reason),
        executed=False,
        synthetic=True,
    )


def _redundant_successful_recall_batch(
    calls: Sequence[UnifiedToolCall],
    all_results: Sequence[UnifiedToolResult],
) -> list[UnifiedToolResult] | None:
    """Match an exact call batch against the successful ledger suffix."""

    if not calls or len(calls) > len(all_results):
        return None
    suffix = list(all_results[-len(calls) :])
    for call, previous in zip(calls, suffix):
        if _cli_tool_call_signature(previous.call) != _cli_tool_call_signature(call):
            return None
        if not previous.success or not previous.output.strip():
            return None
    return suffix


def _openai_exact_successful_recall_results(
    calls: Sequence[UnifiedToolCall],
    all_results: Sequence[UnifiedToolResult],
) -> list[UnifiedToolResult] | None:
    if not calls or len(calls) > len(all_results):
        return None
    suffix = list(all_results[-len(calls) :])
    for call, previous in zip(calls, suffix):
        if _cli_tool_call_signature(previous.call) != _cli_tool_call_signature(call):
            return None
        if (
            not previous.success
            or previous.synthetic
            or previous.executed is not True
            or not previous.output.strip()
        ):
            return None
    return suffix


def _openai_successful_recall_results(
    calls: Sequence[UnifiedToolCall],
    all_results: Sequence[UnifiedToolResult],
    registry: ToolRegistry,
) -> list[UnifiedToolResult | None] | None:
    """Return stable per-call successes while excluding live status reads.

    OpenAI tool rounds may interleave a live status read between repeated
    mutation proposals. Keep a separate signature ledger for this path so the
    read cannot shift a mutation's suffix position. A failed result removes
    that signature and therefore never becomes a retry-suppressing success.
    The CLI helper above intentionally retains its historical exact-suffix
    behavior.
    """

    if not calls:
        return None
    has_dynamic_status_history = any(
        previous.call.tool in _NON_CACHEABLE_SUCCESSFUL_RECALL_TOOLS
        for previous in all_results
    )
    has_dynamic_status_call = any(
        call.tool in _NON_CACHEABLE_SUCCESSFUL_RECALL_TOOLS for call in calls
    )
    if not has_dynamic_status_history and not has_dynamic_status_call:
        return _openai_exact_successful_recall_results(calls, all_results)

    def cacheable(tool_name: str) -> bool:
        if tool_name in _NON_CACHEABLE_SUCCESSFUL_RECALL_TOOLS:
            return False
        try:
            definition = registry.get(tool_name)
        except Exception:
            return False
        side_effect = str(
            getattr(definition, "side_effect", "") or ""
        ).strip().lower()
        return definition is not None and side_effect not in {"", "none"}

    successful: dict[tuple[str, str], UnifiedToolResult] = {}
    for previous in all_results:
        tool = str(previous.call.tool or "")
        if not cacheable(tool):
            continue
        signature = _cli_tool_call_signature(previous.call)
        if (
            previous.success
            and not previous.synthetic
            and previous.executed is True
            and previous.output.strip()
        ):
            successful[signature] = previous
        else:
            successful.pop(signature, None)
    aligned = [
        (
            None
            if not cacheable(call.tool)
            else successful.get(_cli_tool_call_signature(call))
        )
        for call in calls
    ]
    return aligned if any(previous is not None for previous in aligned) else None


def _is_redundant_successful_cli_recall(
    calls: Sequence[UnifiedToolCall],
    all_results: Sequence[UnifiedToolResult],
) -> bool:
    return _redundant_successful_recall_batch(calls, all_results) is not None


def _redundant_cli_recall_final_output(
    calls: Sequence[UnifiedToolCall],
    all_results: Sequence[UnifiedToolResult],
) -> str:
    outputs: list[str] = []
    for call in calls:
        signature = _cli_tool_call_signature(call)
        for previous in reversed(all_results):
            if _cli_tool_call_signature(previous.call) == signature and previous.success:
                outputs.append(previous.output)
                break
    return "\n".join(output for output in outputs if output).strip()


def _tool_has_write_side_effect(registry: ToolRegistry, tool_name: str) -> bool:
    """True unless the tool is a known read-only / live-status read.

    Unknown or unclassifiable tools are treated as writes (fail closed).
    """

    if tool_name in _NON_CACHEABLE_SUCCESSFUL_RECALL_TOOLS:
        return False
    try:
        definition = registry.get(tool_name)
    except Exception:
        definition = None
    if definition is None:
        return True
    side_effect = str(getattr(definition, "side_effect", "") or "").strip().lower()
    return side_effect not in {"none"}


def _plan_call_identity_fence(
    calls: Sequence[UnifiedToolCall],
    arg_errors: Sequence[str],
    dispatched_intents: dict[str, tuple[str, bool, str]],
    identity_check: Callable[[str, str, dict[str, Any]], str | None] | None,
    registry: ToolRegistry | None = None,
    reconciled: dict[int, str] | None = None,
) -> list[str]:
    """Return a per-call rejection reason (``""`` = may dispatch).

    Rejections never rename an id or replay an earlier operation:
    * unusable arguments are rejected outright;
    * one id carrying different intents in a batch rejects every such call;
    * the same id with a different intent than an earlier dispatch is rejected;
    * an identical re-delivery is left to the existing successful-recall
      suppression (mutations are never auto replayed there);
    * the optional durable check (service receipt identity) can reject too, and
      its failure fails closed.
    """

    fingerprints = [
        _tool_call_intent_fingerprint(call.tool, call.arguments)
        if not arg_errors[index]
        else f"invalid:{arg_errors[index]}"
        for index, call in enumerate(calls)
    ]
    by_id: dict[str, set[str]] = {}
    for call, fingerprint in zip(calls, fingerprints):
        call_id = call.call_id.strip()
        if call_id:
            by_id.setdefault(call_id, set()).add(fingerprint)

    def _is_write(tool_name: str) -> bool:
        return registry is not None and _tool_has_write_side_effect(
            registry, tool_name
        )

    reasons: list[str] = []
    batch_writes: set[str] = set()
    for index, call in enumerate(calls):
        call_id = call.call_id.strip()
        if arg_errors[index]:
            reasons.append("invalid_tool_arguments")
            continue
        if not call_id:
            # Without a provider id nothing can bind this call to a durable
            # identity. With a durable check active, fail closed instead of
            # inventing an id; otherwise keep the id-less call unchanged.
            reasons.append("missing_call_id" if identity_check is not None else "")
            continue
        if len(by_id[call_id]) > 1:
            reasons.append("call_id_conflict_in_batch")
            continue
        prior = dispatched_intents.get(call_id)
        if prior is not None and prior[0] != fingerprints[index]:
            reasons.append("call_id_conflict_with_prior_dispatch")
            continue
        if prior is not None and not prior[1] and _is_write(call.tool):
            # An attempted write that failed may have partial effects: the
            # same call is never replayed automatically.
            reasons.append("duplicate_write_not_replayed")
            continue
        if (
            prior is not None
            and prior[1]
            and reconciled is not None
            and _is_write(call.tool)
        ):
            # The turn remembers every executed write outcome (not just the
            # latest batch): an exact retry reuses the success, never replays.
            reconciled[index] = prior[2]
            reasons.append("reconciled_prior_success")
            continue
        if _is_write(call.tool):
            # The same write delivered twice in one batch is one operation:
            # never auto replay it under the same (or a new) id.
            if call_id in batch_writes:
                reasons.append("duplicate_write_in_batch_not_replayed")
                continue
            batch_writes.add(call_id)
        reason = ""
        if identity_check is not None:
            try:
                # Keep the verdict object: it may carry the stored result.
                reason = (
                    identity_check(call_id, call.tool, dict(call.arguments)) or ""
                )
            except Exception:  # noqa: BLE001 - never log/echo the cause
                reason = "identity_check_failed"
        if reason == "already_executed_same_intent":
            # Proven same invocation that already succeeded durably. A
            # read-only poll may refresh; a write is never replayed and is
            # reconciled with the stored successful result.
            if not _is_write(call.tool):
                reason = ""
            elif reconciled is not None and hasattr(reason, "result"):
                reconciled[index] = str(reason.result or "")
                reason = "reconciled_prior_success"
            else:
                reason = "duplicate_write_not_replayed"
        elif reason in {
            "executed_state_unproven",
            "executed_failed_unreconciled",
            "claim_pending_uncertain",
        }:
            reason = "duplicate_write_not_replayed" if _is_write(call.tool) else ""
        reasons.append(reason)
    return reasons


def run_openai_compatible_turn_loop(
    *,
    initial_messages: list[dict[str, Any]],
    assistant_message: Any,
    api_kwargs: dict[str, Any],
    registry: ToolRegistry,
    create_completion: Callable[[dict[str, Any]], Any],
    log_prefix: str = "UnifiedTurn",
    max_rounds: int = 5,
    max_tool_result_chars: int | None = None,
    message_content: Callable[[Any], str] | None = None,
    config: Any | None = None,
    client: Any | None = None,
    user_input: str | None = None,
    enforce_tool_policy: bool = True,
    final_response_check: (
        Callable[[str, Sequence[UnifiedToolResult], int], str | None] | None
    ) = None,
    skip_final_response_check_on_empty: bool = False,
    event_callback: SyncStreamEmitter | None = None,
    turn_ledger: UnifiedTurnLedger | None = None,
    restore_tool_arguments: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    before_provider_call: Callable[[dict[str, Any]], bool] | None = None,
    after_provider_call: Callable[[Any | None], Any] | None = None,
    tool_call_identity_check: Callable[[str, str, dict[str, Any]], str | None]
    | None = None,
) -> UnifiedTurnResult:
    """Run a Codex-style OpenAI-compatible tool loop.

    ``event_callback`` が渡された場合だけ、ツール往復ラウンドの通常テキストを
    ``assistant_text``、reasoning を ``thinking`` として同期発行する。
    """

    effective_user_input = user_input or _last_user_message_content(initial_messages)
    # Turn-local fence: provider call id -> intent fingerprint of calls that
    # were actually dispatched. Never shared across turns, runs, or owners.
    dispatched_intents: dict[str, tuple[str, bool, str]] = {}
    claim_hook = getattr(tool_call_identity_check, "claim", None)
    settle_hook = getattr(tool_call_identity_check, "settle", None)
    # ``id(message)`` はオブジェクト解放後にアドレスが再利用されるため、
    # 別メッセージを無言でスキップしうる。round index をキーにして誤爆を避ける。
    emitted_thinking_rounds: set[int] = set()

    def _emit_thinking_once(message: Any, round_index: int) -> None:
        """同じラウンドについて thinking を二重発行しない。"""
        if event_callback is None or message is None:
            return
        marker = int(round_index)
        if marker in emitted_thinking_rounds:
            return
        emitted_thinking_rounds.add(marker)
        emit_thinking(
            event_callback,
            thinking_text_from_message(message),
            round_index=round_index,
        )

    router = RegistryToolRouter(
        registry,
        log_prefix=log_prefix,
        config=config,
        client=client,
        user_input=effective_user_input,
        enforce_tool_policy=enforce_tool_policy,
    )
    current_messages = list(initial_messages)
    # ``current_messages`` is the canonical turn transcript returned to the
    # caller.  Follow-up requests send a copy whose older tool results are
    # deterministically reduced once the turn grows past the configured
    # within-turn budget (see ``turn_context_shaping``).
    from .turn_context_shaping import WithinTurnContextShaper, tools_payload_chars

    try:
        context_shaper = WithinTurnContextShaper.from_config(
            config,
            label=log_prefix or "unified",
        )
    except Exception:  # noqa: BLE001 - shaping must never block a turn
        logger.warning("within-turn context shaper unavailable", exc_info=True)
        context_shaper = WithinTurnContextShaper(enabled=False)
    reserved_tool_chars = tools_payload_chars(api_kwargs.get("tools"))

    def _shape_outgoing(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return context_shaper.shape_chat_messages(
            messages,
            reserved_chars=reserved_tool_chars,
        )

    current_message = assistant_message
    current_tool_calls = list(getattr(assistant_message, "tool_calls", None) or [])
    current_content = openai_message_content(assistant_message, message_content)
    ledger = turn_ledger or _ACTIVE_TURN_LEDGER.get() or UnifiedTurnLedger()
    all_results = ledger.results
    display_results = list(all_results)
    terminal_failure: GenerationFailure | None = None
    terminal_reason = ""
    terminal_status_message = ""

    def _budget_stop() -> UnifiedTurnResult:
        return UnifiedTurnResult(
            final_output=CLI_TOOL_LOOP_FAILURE_MESSAGE,
            tool_results=display_results,
            rounds=0,
            stopped_reason="budget_exhausted",
            audit_tool_results=list(all_results),
        )

    def _create_provider_completion(payload: dict[str, Any]) -> Any | None:
        if before_provider_call is not None and not before_provider_call(payload):
            return None
        response: Any | None = None
        try:
            response = create_completion(payload)
        except BaseException:
            if after_provider_call is not None:
                after_provider_call(None)
            raise
        # A transport exception is still a provider invocation boundary; the
        # ``None`` callback above releases its reservation and marks the
        # receipt unknown rather than treating the failure as zero spend.
        if after_provider_call is not None:
            after_provider_call(response)
        return response

    def _restore_arguments_for_tool(
        callback: Callable[..., dict[str, Any]],
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Pass the tool name when supported, retaining legacy callbacks."""

        try:
            restored = callback(dict(arguments), tool_name=tool_name)
        except TypeError:
            # Embedders may still provide a text-only one-argument callback.
            restored = callback(dict(arguments))
        # A callback may intentionally return ``None`` when no alias
        # restoration is needed.  Keep the provider's original arguments
        # instead of converting None to a TypeError.
        if restored is None:
            return dict(arguments)
        try:
            return dict(restored)
        except (TypeError, ValueError):
            return dict(arguments)

    for round_index in range(1, max_rounds + 1):
        # native_runtime と揃えて round は 0 始まりで配信する。
        emit_round = round_index - 1
        _emit_thinking_once(current_message, emit_round)
        if not current_tool_calls:
            continuation_prompt = (
                final_response_check(current_content, all_results, round_index)
                if final_response_check
                and not (skip_final_response_check_on_empty and not current_content.strip())
                else None
            )
            if continuation_prompt:
                current_messages.append(
                    serialize_openai_assistant_message(
                        current_message,
                        registry=registry,
                    )
                )
                current_messages.append(
                    {"role": "user", "content": continuation_prompt}
                )
                follow_up_kwargs = dict(api_kwargs)
                follow_up_kwargs["messages"] = _shape_outgoing(current_messages)
                if "tool_choice" in follow_up_kwargs:
                    follow_up_kwargs["tool_choice"] = _follow_up_tool_choice(
                        follow_up_kwargs["tool_choice"]
                    )
                response = _create_provider_completion(follow_up_kwargs)
                if response is None:
                    return _budget_stop()
                message = response.choices[0].message
                current_message = message
                current_tool_calls = list(getattr(message, "tool_calls", None) or [])
                current_content = openai_message_content(message, message_content)
                continue
            return UnifiedTurnResult(
                final_output=current_content,
                tool_results=display_results,
                rounds=round_index - 1,
                stopped_reason="final",
                audit_tool_results=list(all_results),
                messages=[
                    *current_messages,
                    serialize_openai_assistant_message(
                        current_message,
                        registry=registry,
                    ),
                ],
            )

        # ツール呼び出しを伴うラウンドの通常テキストは途中経過として配信する。
        tool_names: tuple[str, ...] = ()
        get_names = getattr(registry, "get_names", None)
        if callable(get_names):
            try:
                tool_names = tuple(str(name) for name in (get_names() or ()) if name)
            except Exception:
                # Legacy duck registries may expose only ``__contains__`` and
                # ``execute``.  Fall back to the names in this authoritative
                # tool-call batch instead of requiring the full registry API.
                tool_names = ()
        if not tool_names:
            tool_names = tuple(
                name
                for raw_call in current_tool_calls
                for name, _, _ in [_openai_tool_call_function_or_error(raw_call)]
                if name and observable_tool_name(registry, name) == name
            )
        emit_assistant_text(
            event_callback,
            current_content,
            round_index=emit_round,
            tool_names=tool_names,
        )

        current_messages.append(
            serialize_openai_assistant_message(
                current_message,
                registry=registry,
            )
        )

        call_arg_errors: list[str] = []
        calls = []
        for raw_call in current_tool_calls:
            name, arguments, arg_error = _openai_tool_call_function_or_error(raw_call)
            call_arg_errors.append(arg_error)
            calls.append(
                UnifiedToolCall(
                    # Keep the raw provider name for authoritative registry
                    # lookup; only events/results/messages use the safe projection.
                    tool=name,
                    arguments=(
                        _restore_arguments_for_tool(
                            restore_tool_arguments,
                            observable_tool_name(registry, name),
                            dict(arguments),
                        )
                        if restore_tool_arguments is not None and not arg_error
                        else arguments
                    ),
                    call_id=openai_tool_call_id(raw_call),
                    raw=raw_call,
                )
            )
        reconciled_outputs: dict[int, str] = {}
        batch_results: dict[str, UnifiedToolResult] = {}
        blocked_reasons = _plan_call_identity_fence(
            calls,
            call_arg_errors,
            dispatched_intents,
            tool_call_identity_check,
            registry,
            reconciled_outputs,
        )
        ledger_snapshot = tuple(all_results)
        redundant_batch = _openai_successful_recall_results(
            calls,
            ledger_snapshot,
            registry,
        )
        results: list[UnifiedToolResult] = []
        model_results: list[UnifiedToolResult] = []
        suppressed_results: list[UnifiedToolResult] = []
        def _fence_call(
            call: UnifiedToolCall,
            call_index: int,
            blocked_reason: str,
            claim_result: str | None = None,
        ) -> None:
            # Fail closed before authorization/registry execution. The
            # call is never renamed or replayed as another operation.
            blocked = replace(
                _synthetic_not_executed_result(
                    router,
                    call,
                    reason=blocked_reason,
                ),
                failure_group="identity_fence",
                intent_digest=_raw_intent_digest(call.arguments),
            )
            reconciled_output: str | None = claim_result
            if claim_result is not None:
                pass
            elif blocked_reason == "reconciled_prior_success":
                reconciled_output = reconciled_outputs.get(call_index, "")
            elif blocked_reason == "duplicate_write_in_batch_not_replayed":
                first = batch_results.get(call.call_id.strip())
                if first is not None and first.success and first.executed:
                    reconciled_output = first.output
            if reconciled_output is not None:
                # Same proven invocation already succeeded: report that
                # success without a second side effect. Audit-only (not
                # ledgered as a new execution).
                blocked = replace(
                    blocked,
                    output=reconciled_output,
                    success=True,
                    error="",
                    status_message="同一呼び出しの実行済み結果を再利用しました（再実行なし）",
                )
            model_results.append(blocked)
            if blocked_reason == "reconciled_prior_success" and claim_result is None:
                # Turn-memory reconcile behaves like the historical redundant
                # recall: model-visible only, no extra display entry.
                suppressed_results.append(blocked)
            else:
                display_results.append(blocked)
            logger.warning(
                "[%s] Tool call rejected before dispatch (reason=%s)",
                log_prefix,
                blocked_reason,
            )
            _emit_sync(
                event_callback,
                "tool_end",
                {
                    "tool": blocked.call.tool,
                    "tool_args": blocked.call.arguments,
                    "message": blocked.status_message,
                    "operation_id": blocked.call.call_id,
                    "tool_call_id": blocked.call.call_id,
                    "tool_result": {
                        "tool": blocked.call.tool,
                        "arguments": blocked.call.arguments,
                        "output": blocked.output,
                        "error": blocked.error,
                        "success": bool(blocked.success),
                        "executed": False,
                        "synthetic": True,
                        "not_executed_reason": blocked.error,
                        "tool_call_id": blocked.call.call_id,
                    },
                },
            )

        for call_index, call in enumerate(calls):
            blocked_reason = blocked_reasons[call_index]
            if blocked_reason:
                _fence_call(call, call_index, blocked_reason)
                continue
            previous = (
                redundant_batch[call_index]
                if redundant_batch is not None
                else None
            )
            if previous is not None:
                safe_call = UnifiedToolCall(
                    tool=observable_tool_name(router.registry, call.tool),
                    arguments=_observable_tool_arguments(
                        router.registry,
                        call.tool,
                        call.arguments,
                    ),
                    call_id=call.call_id,
                )
                suppressed = UnifiedToolResult(
                    call=safe_call,
                    output=previous.output,
                    success=True,
                    elapsed_ms=previous.elapsed_ms,
                )
                model_results.append(suppressed)
                suppressed_results.append(suppressed)
                logger.info(
                    "[%s] Suppressed redundant successful OpenAI tool recall: %s",
                    log_prefix,
                    observable_tool_name(router.registry, call.tool),
                )
                continue

            claim_held = False
            call_key = call.call_id.strip()
            if (
                claim_hook is not None
                and call_key
                and _tool_has_write_side_effect(router.registry, call.tool)
            ):
                # Reserve the run/call id for this exact intent before any
                # effect. Only the claim owner may dispatch; everything else
                # (conflict, pending/uncertain, executed failure) fails closed.
                try:
                    verdict = claim_hook(call_key, call.tool, dict(call.arguments))
                except Exception:  # noqa: BLE001 - never echo the cause
                    verdict = "identity_check_failed"
                if verdict:
                    if str(verdict) == "already_executed_same_intent" and hasattr(
                        verdict, "result"
                    ):
                        _fence_call(
                            call,
                            call_index,
                            "reconciled_prior_success",
                            claim_result=str(verdict.result or ""),
                        )
                    elif str(verdict) in {
                        "claim_pending_uncertain",
                        "executed_failed_unreconciled",
                        "executed_state_unproven",
                    }:
                        _fence_call(call, call_index, "duplicate_write_not_replayed")
                    else:
                        _fence_call(call, call_index, str(verdict))
                    continue
                claim_held = True

            operation_id = call.call_id.strip() or str(uuid.uuid4())
            observable_arguments = _observable_tool_arguments(
                router.registry,
                call.tool,
                call.arguments,
            )
            observable_name = observable_tool_name(router.registry, call.tool)
            _emit_sync(
                event_callback,
                "tool_start",
                {
                    "tool": observable_name,
                    "tool_args": observable_arguments,
                    "message": f"Running {observable_name}",
                    "operation_id": operation_id,
                },
            )
            result = router.execute(call)
            result = replace(
                result,
                intent_digest=_raw_intent_digest(call.arguments),
            )
            if claim_held and settle_hook is not None:
                # Authoritative outcome is durable before the next call. A
                # failed settlement leaves the claim pending (uncertain), so
                # the same call is never replayed.
                try:
                    settle_hook(
                        call_key,
                        call.tool,
                        dict(call.arguments),
                        bool(result.executed),
                        bool(result.success),
                        result.output,
                    )
                except Exception:  # noqa: BLE001 - never echo the cause
                    logger.warning(
                        "[%s] Tool call settlement failed; claim left pending",
                        log_prefix,
                    )
            if result.executed and call.call_id.strip():
                # Only dispatched calls bind an id; a validation-failed
                # (never executed) call leaves its id free for a repair.
                dispatched_intents[call.call_id.strip()] = (
                    _tool_call_intent_fingerprint(call.tool, call.arguments),
                    bool(result.success),
                    result.output,
                )
            if call.call_id.strip():
                batch_results[call.call_id.strip()] = result
            results.append(result)
            model_results.append(result)
            ledger.record(result)
            display_results.append(result)
            tool_result_payload: dict[str, Any] = {
                "tool": observable_name,
                "arguments": observable_arguments,
                "output": _bounded_tool_end_output(result),
                "error": (
                    _bounded_tool_end_text(result.error)
                    if not result.success
                    else ""
                ),
                # Preserve the router's authoritative status.  The event
                # consumer keeps a legacy error-based fallback for older
                # producers, but an explicit false must never be inferred as
                # success merely because its error field is empty.
                "success": bool(result.success),
                "executed": bool(result.executed),
                "synthetic": result.synthetic,
            }
            if result.blocked_reason:
                tool_result_payload["blocked_reason"] = _bounded_tool_end_text(
                    result.blocked_reason
                )
            tool_result_payload["tool_call_id"] = operation_id
            _emit_sync(
                event_callback,
                "tool_end",
                {
                    "tool": observable_name,
                    "tool_args": observable_arguments,
                    "message": _tool_status_message(
                        result,
                        success_message=f"Completed {observable_name}",
                    ),
                    "executed": bool(result.executed),
                    "synthetic": result.synthetic,
                    "operation_id": operation_id,
                    "tool_call_id": operation_id,
                    "tool_result": tool_result_payload,
                },
            )

            if result.generation_failure is not None or result.turn_terminal:
                # The framework-owned breaker is terminal for this turn. Keep
                # remaining provider call IDs in the transcript, but never
                # execute them or add them to the execution ledger.
                terminal_failure = result.generation_failure
                terminal_reason = (
                    result.terminal_reason
                    or (terminal_failure.kind if terminal_failure else "tool_terminal")
                )
                terminal_status_message = (
                    result.status_message
                    or (terminal_failure.user_message if terminal_failure else "ツール実行を停止しました")
                )
                for skipped_call in calls[call_index + 1 :]:
                    skipped = _synthetic_not_executed_result(
                        router,
                        skipped_call,
                        reason=_terminal_skip_reason(terminal_reason),
                    )
                    model_results.append(skipped)
                    display_results.append(skipped)
                    skipped_id = skipped.call.call_id
                    _emit_sync(
                        event_callback,
                        "tool_end",
                        {
                            "tool": skipped.call.tool,
                            "tool_args": skipped.call.arguments,
                            "message": skipped.status_message,
                            "operation_id": skipped_id,
                            "tool_call_id": skipped_id,
                            "tool_result": {
                                "tool": skipped.call.tool,
                                "arguments": skipped.call.arguments,
                                "output": skipped.output,
                                "error": skipped.error,
                                "success": False,
                                "executed": False,
                                "synthetic": True,
                                "not_executed_reason": skipped.error,
                                "tool_call_id": skipped_id,
                            },
                        },
                    )
                break

        for result in model_results:
            model_payload = model_tool_result_payload(
                tool_name=result.call.tool,
                output=result.model_output,
                user_input=effective_user_input,
                max_chars=max_tool_result_chars,
                config=config,
                legacy_clip=clip_text,
            )
            current_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": result.call.call_id,
                    "content": model_payload.text,
                }
            )

        if terminal_failure is not None or terminal_reason:
            return UnifiedTurnResult(
                final_output=(
                    terminal_failure.user_message
                    if terminal_failure is not None
                    else terminal_status_message
                ),
                tool_results=display_results,
                rounds=round_index,
                stopped_reason=terminal_reason,
                messages=list(current_messages),
                audit_tool_results=list(all_results),
                generation_failure=terminal_failure,
            )

        if suppressed_results and not results:
            final_kwargs = dict(api_kwargs)
            final_messages = [
                *current_messages,
                {
                    "role": "user",
                    "content": SUPPRESSED_TOOL_FINAL_ANSWER_INSTRUCTION,
                },
            ]
            final_kwargs["messages"] = final_messages
            final_kwargs.pop("tools", None)
            final_kwargs.pop("tool_choice", None)
            response = _create_provider_completion(final_kwargs)
            if response is None:
                return _budget_stop()
            message = response.choices[0].message
            final_content = openai_message_content(message, message_content)
            _emit_thinking_once(message, emit_round + 1)
            repeated_tool_calls = list(getattr(message, "tool_calls", None) or [])
            if repeated_tool_calls:
                final_content = (
                    "The requested tool operation was already completed; "
                    "no duplicate execution was performed."
                )
                final_message = {"role": "assistant", "content": final_content}
            else:
                final_message = serialize_openai_assistant_message(
                    message,
                    registry=registry,
                )
            return UnifiedTurnResult(
                final_output=final_content,
                tool_results=display_results,
                rounds=round_index,
                stopped_reason=(
                    "redundant_tool_call_repeated"
                    if repeated_tool_calls
                    else "redundant_tool_call_suppressed"
                ),
                messages=[
                    *final_messages,
                    final_message,
                ],
                audit_tool_results=list(all_results),
            )

        follow_up_kwargs = dict(api_kwargs)
        follow_up_kwargs["messages"] = _shape_outgoing(current_messages)
        if "tool_choice" in follow_up_kwargs:
            follow_up_kwargs["tool_choice"] = _follow_up_tool_choice(
                follow_up_kwargs["tool_choice"]
            )

        response = _create_provider_completion(follow_up_kwargs)
        if response is None:
            return _budget_stop()
        message = response.choices[0].message
        current_message = message
        next_tool_calls = getattr(message, "tool_calls", None)
        current_content = openai_message_content(message, message_content)
        # 次ラウンドへ進む場合はループ先頭の重複排除で1回だけ配信される。
        _emit_thinking_once(current_message, emit_round + 1)
        if next_tool_calls:
            current_tool_calls = list(next_tool_calls)
            continue
        continuation_prompt = (
            final_response_check(current_content, all_results, round_index)
            if final_response_check
            and not (skip_final_response_check_on_empty and not current_content.strip())
            else None
        )
        if continuation_prompt:
            current_messages.append(
                serialize_openai_assistant_message(
                    current_message,
                    registry=registry,
                )
            )
            current_messages.append({"role": "user", "content": continuation_prompt})
            follow_up_kwargs = dict(api_kwargs)
            follow_up_kwargs["messages"] = _shape_outgoing(current_messages)
            if "tool_choice" in follow_up_kwargs:
                follow_up_kwargs["tool_choice"] = _follow_up_tool_choice(
                    follow_up_kwargs["tool_choice"]
                )
            response = _create_provider_completion(follow_up_kwargs)
            if response is None:
                return _budget_stop()
            message = response.choices[0].message
            current_message = message
            current_tool_calls = list(getattr(message, "tool_calls", None) or [])
            current_content = openai_message_content(message, message_content)
            # 継続要求で差し替えた新しいメッセージは同じ round 番号のまま
            # 次ループ先頭へ渡るため、その round の発行済みマークを外して
            # 新しい思考が落ちないようにする。
            emitted_thinking_rounds.discard(emit_round + 1)
            continue

        return UnifiedTurnResult(
            final_output=current_content,
            tool_results=display_results,
            rounds=round_index,
            stopped_reason="final",
            messages=[
                *current_messages,
                serialize_openai_assistant_message(
                    current_message,
                    registry=registry,
                ),
            ],
            audit_tool_results=list(all_results),
        )

    logger.warning("[%s] Unified turn loop exceeded max rounds", log_prefix)
    return UnifiedTurnResult(
        final_output=current_content or "",
        tool_results=display_results,
        rounds=max_rounds,
        stopped_reason="max_rounds",
        messages=[
            *current_messages,
            serialize_openai_assistant_message(
                current_message,
                registry=registry,
            ),
        ],
        audit_tool_results=list(all_results),
    )


def run_cli_tool_call_loop(
    *,
    original_input: str,
    initial_output: str,
    registry: ToolRegistry,
    parse_tool_calls: Callable[[str], list[dict[str, Any]]],
    execute_follow_up: Callable[[str], tuple[bool, str]],
    build_follow_up_prompt: Callable[[str, str, str], str],
    log_prefix: str = "UnifiedCLITurn",
    # Explicit callers (the main CLI client) provide profile-specific
    # budgets.  Specialist CLI delegation uses this default, so keep it at
    # the bounded work budget rather than the old chat-sized five rounds.
    max_rounds: int = 12,
    max_tool_result_chars: int | None = None,
    config: Any | None = None,
    client: Any | None = None,
    user_input: str | None = None,
    enforce_tool_policy: bool = True,
    event_callback: Callable[[str, dict[str, Any]], Any] | None = None,
    final_response_check: (
        Callable[[str, Sequence[UnifiedToolResult], int], str | None] | None
    ) = None,
    should_cancel: Callable[[], bool] | None = None,
) -> UnifiedTurnResult:
    """Run a CLI backend tool loop with repeated follow-up support."""

    router = RegistryToolRouter(
        registry,
        log_prefix=log_prefix,
        config=config,
        client=client,
        user_input=user_input or original_input,
        enforce_tool_policy=enforce_tool_policy,
    )
    current_output = str(initial_output or "")
    all_results: list[UnifiedToolResult] = []
    display_results: list[UnifiedToolResult] = []
    terminal_failure: GenerationFailure | None = None
    terminal_reason = ""
    terminal_status_message = ""

    # ``max_rounds`` limits *tool executions*, not the follow-up response
    # that is produced after the last tool result.  Keep one parse-only pass
    # after the final allowed tool round so a normal final answer is not
    # misclassified as exhausted.  The extra pass must never execute another
    # tool call: a pending marker at that point is an explicit exhaustion.
    for round_index in range(1, max_rounds + 2):
        if should_cancel and should_cancel():
            return UnifiedTurnResult(
                final_output=current_output,
                tool_results=display_results,
                rounds=round_index - 1,
                stopped_reason="cancelled",
                audit_tool_results=list(all_results),
            )
        parsed_calls = parse_tool_calls(current_output)
        if not parsed_calls:
            if round_index > max_rounds:
                continuation_prompt = (
                    final_response_check(current_output, all_results, round_index)
                    if final_response_check
                    else None
                )
                if continuation_prompt:
                    logger.warning(
                        "[%s] CLI final response still requires a tool after max rounds",
                        log_prefix,
                    )
                    return UnifiedTurnResult(
                        final_output=CLI_TOOL_LOOP_FAILURE_MESSAGE,
                        tool_results=display_results,
                        rounds=max_rounds,
                        stopped_reason="max_rounds",
                        audit_tool_results=list(all_results),
                    )
                return UnifiedTurnResult(
                    final_output=current_output,
                    tool_results=display_results,
                    rounds=max_rounds,
                    stopped_reason="final",
                    audit_tool_results=list(all_results),
                )
            continuation_prompt = (
                final_response_check(current_output, all_results, round_index)
                if final_response_check
                else None
            )
            if continuation_prompt:
                follow_up = build_follow_up_prompt(
                    original_input,
                    current_output,
                    continuation_prompt,
                )
                _emit_sync(
                    event_callback,
                    "status_update",
                    {
                        "status": "cli_tool_required",
                        "message": "Required tool check requested a CLI follow-up",
                    },
                )
                success, follow_up_output = execute_follow_up(follow_up)
                if not success:
                    logger.error(
                        "[%s] CLI required-tool follow-up failed: %s",
                        log_prefix,
                        follow_up_output,
                    )
                    return UnifiedTurnResult(
                        final_output=CLI_TOOL_LOOP_FAILURE_MESSAGE,
                        tool_results=display_results,
                        rounds=round_index - 1,
                        stopped_reason="follow_up_error",
                        audit_tool_results=list(all_results),
                    )
                current_output = str(follow_up_output or "")
                continue
            return UnifiedTurnResult(
                final_output=current_output,
                tool_results=display_results,
                rounds=round_index - 1,
                stopped_reason="final",
                audit_tool_results=list(all_results),
            )

        if round_index > max_rounds:
            # The model returned another tool marker after the last permitted
            # round.  Do not execute it; the caller must surface a terminal
            # exhausted state rather than treating the marker as an answer.
            logger.warning(
                "[%s] CLI tool loop exceeded max rounds with a pending tool call",
                log_prefix,
            )
            return UnifiedTurnResult(
                final_output=CLI_TOOL_LOOP_FAILURE_MESSAGE,
                tool_results=display_results,
                rounds=max_rounds,
                stopped_reason="max_rounds",
                audit_tool_results=list(all_results),
            )

        calls = [
            UnifiedToolCall(
                # Do not let the public ``unknown_tool`` marker collide with a
                # legitimately registered tool of that name.
                tool=call.tool,
                arguments=dict(call.arguments),
                call_id=call.call_id,
                raw=call.raw,
            )
            for call in cli_tool_calls_from_parsed(parsed_calls)
        ]
        if _is_redundant_successful_cli_recall(calls, all_results):
            final_output = _redundant_cli_recall_final_output(calls, all_results)
            logger.info(
                "[%s] Suppressed redundant successful CLI tool recall",
                log_prefix,
            )
            return UnifiedTurnResult(
                final_output=final_output or current_output,
                tool_results=display_results,
                rounds=round_index - 1,
                stopped_reason="redundant_tool_call_suppressed",
                audit_tool_results=list(all_results),
            )
        logger.info(
            "[%s] Executing %s CLI tool call(s)",
            log_prefix,
            len(calls),
        )
        results: list[UnifiedToolResult] = []
        for call_index, call in enumerate(calls):
            if should_cancel and should_cancel():
                return UnifiedTurnResult(
                    final_output=current_output,
                    tool_results=display_results,
                    rounds=round_index - 1,
                    stopped_reason="cancelled",
                    audit_tool_results=list(all_results),
                )
            observable_arguments = _observable_tool_arguments(
                router.registry,
                call.tool,
                call.arguments,
            )
            observable_name = observable_tool_name(router.registry, call.tool)
            _emit_sync(
                event_callback,
                "tool_start",
                {
                    "tool": observable_name,
                    "tool_args": observable_arguments,
                    "message": f"Running {observable_name}",
                },
            )
            result = router.execute(call)
            results.append(result)
            display_results.append(result)
            if should_cancel and should_cancel():
                return UnifiedTurnResult(
                    final_output=current_output,
                    tool_results=display_results,
                    rounds=round_index,
                    stopped_reason="cancelled",
                    audit_tool_results=[*all_results, *results],
                )
            _emit_sync(
                event_callback,
                "tool_end",
                {
                    "tool": observable_name,
                    "tool_args": observable_arguments,
                    "message": _tool_status_message(
                        result,
                        success_message=f"Completed {observable_name}",
                    ),
                    "tool_result": {
                        "tool": observable_name,
                        "arguments": observable_arguments,
                        "output": result.model_output,
                        "error": result.error if not result.success else "",
                        # Keep an explicit failure marker even when a policy
                        # block has no legacy error string.
                        "success": bool(result.success),
                        "executed": bool(result.executed),
                        "synthetic": result.synthetic,
                        **(
                            {"blocked_reason": result.blocked_reason}
                            if result.blocked_reason
                            else {}
                        ),
                    },
                },
            )
            if result.generation_failure is not None or result.turn_terminal:
                terminal_failure = result.generation_failure
                terminal_reason = (
                    result.terminal_reason
                    or (terminal_failure.kind if terminal_failure else "tool_terminal")
                )
                terminal_status_message = (
                    result.status_message
                    or (terminal_failure.user_message if terminal_failure else "ツール実行を停止しました")
                )
                for skipped_call in calls[call_index + 1 :]:
                    skipped = _synthetic_not_executed_result(
                        router,
                        skipped_call,
                        reason=_terminal_skip_reason(terminal_reason),
                    )
                    display_results.append(skipped)
                    _emit_sync(
                        event_callback,
                        "tool_end",
                        {
                            "tool": skipped.call.tool,
                            "tool_args": skipped.call.arguments,
                            "message": skipped.status_message,
                            "tool_result": {
                                "tool": skipped.call.tool,
                                "arguments": skipped.call.arguments,
                                "output": skipped.output,
                                "error": skipped.error,
                                "success": False,
                                "executed": False,
                                "synthetic": True,
                                "not_executed_reason": skipped.error,
                            },
                        },
                    )
                break
        all_results.extend(results)

        if terminal_failure is not None or terminal_reason:
            return UnifiedTurnResult(
                final_output=(
                    terminal_failure.user_message
                    if terminal_failure is not None
                    else terminal_status_message
                ),
                tool_results=display_results,
                rounds=round_index,
                stopped_reason=terminal_reason,
                audit_tool_results=list(all_results),
                generation_failure=terminal_failure,
            )

        follow_up = build_follow_up_prompt(
            original_input,
            current_output,
            format_cli_follow_up_tool_context(
                results,
                all_results,
                original_input=original_input,
                max_chars=max_tool_result_chars,
                config=config,
                user_input=user_input or original_input,
            ),
        )
        _emit_sync(
            event_callback,
            "status_update",
            {
                "status": "cli_tool_results_received",
                "message": "Tool results received; continuing CLI turn",
            },
        )
        success, follow_up_output = execute_follow_up(follow_up)
        if not success:
            logger.error(
                "[%s] CLI follow-up failed: %s",
                log_prefix,
                follow_up_output,
            )
            return UnifiedTurnResult(
                final_output=CLI_TOOL_LOOP_FAILURE_MESSAGE,
                tool_results=display_results,
                rounds=round_index,
                stopped_reason="follow_up_error",
                audit_tool_results=list(all_results),
            )

        current_output = str(follow_up_output or "")

    logger.warning("[%s] CLI tool loop exceeded max rounds", log_prefix)
    return UnifiedTurnResult(
        final_output=CLI_TOOL_LOOP_FAILURE_MESSAGE,
        tool_results=display_results,
        rounds=max_rounds,
        stopped_reason="max_rounds",
        audit_tool_results=list(all_results),
    )


def _emit_sync(
    event_callback: Callable[[str, dict[str, Any]], Any] | None,
    event_type: str,
    data: dict[str, Any],
) -> None:
    if event_callback is None:
        return
    try:
        event_callback(event_type, data)
    except GenerationInterrupted:
        raise
    except Exception:
        logger.debug(
            "[UnifiedCLITurn] Event callback failed for %s",
            event_type,
            exc_info=True,
        )


def _last_user_message_content(messages: Sequence[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict)
            ]
            return "\n".join(part for part in parts if part).strip()
    return ""
