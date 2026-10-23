"""Generic local OpenAI-compatible LLM client.

This provider is for already-running local servers such as llama.cpp
llama-server. Optional OpenAI-compatible features such as native tool calling,
response_format, and user-configured extra_body are opt-in compatibility
parameters; mode-specific extra_body is applied automatically where needed.
"""

from __future__ import annotations

import asyncio
import copy
import concurrent.futures
import contextvars
import dataclasses
import hashlib
import inspect
import json
import logging
import math
import os
import re
import threading
import time
import traceback
import urllib.request
from datetime import datetime
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from typing import Any, Callable, Dict, Generator, Iterable, List, Optional, Union
from uuid import uuid4

import httpx
from openai import APIError, OpenAI

from ..config import Config
from ..memory.history import HistoryManager
from ..services.project_context import (
    ProjectContextResolver,
    project_context_enabled_for_client,
    reset_runtime_project_context,
    set_runtime_project_context,
)
from ..services.context_builder import ContextBuilder, ContextBundle
from .manager_parts.context_building import strip_project_context_bundle
from ..services.turn_context import (
    AOITALK_HELP_ISOLATED_SYSTEM_PROMPT,
    bind_context_to_iterator,
    get_turn_context,
)
from ..services.user_settings_service import get_user_custom_instructions_sync
from ..services.outbound_privacy_service import (
    EgressDescriptor,
    OutboundPrivacyGateway,
    PrivacyError,
)
from ..tools.adapters import OpenAIAPIAdapter
from ..tools.registry import ToolRegistry
from .generation_policy import (
    DEFAULT_GENERATION_POLICY,
    GenerationProfile,
    get_client_generation_policy,
    reset_current_generation_policy,
    set_current_generation_policy,
)
from .generation_budget import constrain_model_request_extra_body, current_generation_budget
from .generation_cancellation import (
    AgentDialogueInputPending,
    GenerationCancelled,
    GenerationInterrupted,
    PlanningInteractionTerminated,
    get_current_generation_cancellation,
    register_generation_cancellation,
    raise_if_generation_interrupted,
    release_generation_cancellation,
    reset_current_generation_cancellation,
    run_owned_cli_worker,
    set_current_generation_cancellation,
)
from .generation_error import (
    GenerationErrorKind,
    ModelOutputIncompleteError,
    classify_generation_error,
    empty_response_failure,
)
from .provider_terminal import (
    FAILED_TERMINAL_STATES,
    interrupted_terminal,
    normalize_chat_terminal,
    protocol_error_terminal,
    sanitize_terminal_evidence,
)
from .agentic_completion import (
    agentic_max_rounds,
    format_tool_execution_evidence,
    get_current_compatible_generation_failure,
    reset_current_compatible_generation_failure,
    render_messages_for_review,
    run_agentic_completion_loop_sync,
    set_current_compatible_generation_failure,
    successful_empty_task_search,
    tool_loop_completion_confirmed,
)
from .agent_runtime import (
    DIRECT_PROJECT_TOOL_HINT_NAMES,
    build_tool_hint_context_sync,
    compose_tool_hint_user_message,
    dialogue_provider_wire_max_output_tokens,
    guard_tool_execution_claims,
    project_context_required_read_tool_names,
    run_openai_tool_call_loop,
)
from .unified_turn_runtime import (
    ToolNoProgressGuard,
    activate_tool_no_progress_guard,
    current_tool_no_progress_guard,
)
from .specialist_delegate import (
    reset_runtime_specialist_provider,
    set_runtime_specialist_provider,
)
from .context_budget import (
    TOOL_GENERATION_ALLOWANCE_TOKENS,
    ContextBudget,
    clip_text,
    clip_text_preserve_tail,
    completion_allowance_tokens,
    reduced_context_window_after_overflow,
    resolve_context_budget,
)
from .context_compression import compact_media_tool_result
from .openai_compatible_local_profiles import (
    DEFAULT_OPENAI_COMPATIBLE_LOCAL_BASE_URL,
    llama_cpp_model_profile,
    llama_cpp_profile_capabilities,
    managed_local_runtime_for_model,
    strata_model_profile,
    llama_cpp_thinking_mode,
    llama_cpp_reasoning_effort_metadata,
    llama_cpp_reasoning_effort_request_extra_body,
    openai_compatible_local_base_url,
    openai_compatible_server_profile,
)
from .conversation_context import (
    PromptMessages,
    build_prompt_messages,
    stable_history_window,
    canonical_model_transcript_for_history,
    normalize_usage,
    persist_usage_sync,
    project_prompt_cache_key,
    stable_cache_key,
)
from .context_snapshot import (
    capture_context_manifest_before_context_clear,
    component,
    context_bundle_partition_components,
    context_manifest_metadata,
    message_components,
    reconcile_snapshot,
    context_snapshot_without_manifest_capture,
    snapshot,
    tool_components,
    without_text,
)
from .multimodal import openai_content_parts
from .harness_instructions import (
    COMMON_HARNESS_INSTRUCTIONS,
    clip_keeping_common_block,
    system_clip_limit,
    with_common_harness_instructions,
)
from .prompts import build_unified_instructions
from .provider_capabilities import ProviderCapabilities
from .provider_mode_adapters import (
    FAST_THINKING_MODE_OPTIONS,
    resolve_strata_response_mode,
)
from .runtime_tool_registry import (
    build_runtime_tool_registry,
    build_runtime_tool_registry_for_client,
)
from .tool_packs import (
    LOAD_TOOL_PACK_TOOL_NAME,
    auto_load_packs_for_tool_names,
    ensure_load_tool_pack_tool,
    tool_pack_session_for_client,
)
from .tool_exposure import (
    filter_tools_for_client,
    filtered_registry_for_client,
    get_strict_tool_allowlist,
)
from .turn_stream_events import (
    SyncStreamEmitter,
    _has_incomplete_call_suffix,
    bind_stream_callback_loop,
    emit_thinking,
    looks_like_tool_call_echo,
    make_sync_stream_emitter,
    strip_leading_think_markup as _strip_leading_think_markup,
    thinking_text_from_message,
)
from .tool_call_identity_bridge import (
    bind_tool_identity_owner_loop,
    build_durable_tool_call_identity_check,
    reset_tool_identity_owner_loop,
)
from .tool_policy import (
    PROJECT_COMMAND_CAPABILITIES,
    agent_tool_scope_permits_exposure,
    get_current_agent_tool_scope,
    command_capability_active,
    command_capabilities_from_text,
    mutation_execution_forbidden,
    looks_like_utility_request,
    looks_like_managed_workspace_request,
    looks_like_project_management_request,
    project_management_required_mutation_tools,
    project_progress_review_active,
    get_current_user_input,
    reset_current_user_input,
    sanitize_command_capabilities,
    set_current_user_input,
)

# A constrained native schema must expose the same safe, high-level set for
# an explicit ``workspace_file_operation`` command and for a server-verified
# Project attachment.  Keep this set deliberately narrower than every legacy
# filesystem tool (no native shell/repository mutation tools).
MANAGED_WORKSPACE_NATIVE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "list_workspace_tree",
        "list_directory",
        "read_file",
        "create_workspace_directory",
        "copy_workspace_item",
        "move_workspace_item",
        "docs_read",
        "docs_attach_workspace_file",
        "docs_place_workspace_file",
    }
)

# Utility requests are deterministic and have one authoritative root tool. Keep
# their native schema narrow on Strata so a short question does not make the
# model choose among the whole registry (or answer from prior knowledge).
UTILITY_NATIVE_TOOL_NAMES: frozenset[str] = frozenset(
    {"get_current_time", "get_weather_info", "calculate"}
)

UTILITY_COMPOSITE_ACTION_MARKERS: tuple[str, ...] = (
    "docs",
    "document",
    "task",
    "project",
    "file",
    "workspace",
    "write",
    "update",
    "create",
    "save",
    "add",
    "append",
    "record",
    "send",
    "then",
    "after that",
    "with that",
    "using that",
    "その時刻で",
    "その時間で",
    "時刻で",
    "時間で",
    "書いて",
    "書き込",
    "記録",
    "更新",
    "作成",
    "追加",
    "保存",
    "ファイル",
    "ドキュメント",
    "タスク",
    "プロジェクト",
    "案件",
    "ワークスペース",
    "検索",
    "連携",
)

logger = logging.getLogger(__name__)
_MEMORY_SELECTION_STREAM_CALLBACK: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "aoitalk_local_memory_selection_stream_callback", default=None,
)
_LOCAL_TIMING_CALLBACK: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "aoitalk_local_timing_callback", default=None,
)
_LOCAL_TIMING_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "aoitalk_local_timing_id", default=None,
)
_LOCAL_PROVIDER_SENDER_ATTEMPTS: contextvars.ContextVar[int] = contextvars.ContextVar(
    "aoitalk_local_provider_sender_attempts", default=0,
)
_LOCAL_PROVIDER_REQUEST_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar(
    "aoitalk_local_provider_request_depth", default=0,
)
_LOCAL_PROVIDER_TERMINAL_TRACKING: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "aoitalk_local_provider_terminal_tracking", default=True,
)
_LOCAL_PROVIDER_TURN_ACTIVE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "aoitalk_local_provider_turn_active", default=False,
)


_CATALOG_CAPTURE_ROOT_CONTROL_NAMES: tuple[str, ...] = (
    "delegate_agent_work",
    "get_agent_work_status",
    "send_agent_work_message",
    "answer_agent_work_question",
)
_CATALOG_CAPTURE_WORKER_CONTROL_NAMES: tuple[str, ...] = (
    "ask_user_question",
)
_CATALOG_CAPTURE_MAX_NAMES = 128
_CATALOG_CAPTURE_MAX_NAME_CHARS = 128
_CATALOG_CAPTURE_NULLABLE_TIMING_KEYS = frozenset(
    {
        "sorted_tool_names",
        "catalog_names_sha256",
        "read_chat_session_present",
        "expected_control_names",
        "missing_control_names",
        "total_name_count",
        "list_truncated",
        "catalog_complete",
        "expected_controls_known",
        "content_length",
        "reasoning_length",
        "reasoning_close_retry_effective",
    }
)

DEFAULT_LOCAL_BASE_URL = DEFAULT_OPENAI_COMPATIBLE_LOCAL_BASE_URL
DEFAULT_LOCAL_MODEL = "local-model"
DEFAULT_LOCAL_API_KEY = "dummy"
CONSTRAINED_NATIVE_TOOL_SCHEMA_CHAR_BUDGET = 9000
# 狭コンテキストでスキーマを削る時も、ローカルDocs・過去会話の意味検索は
# registry順で後方に並んでいても先に残す。
CONSTRAINED_PRIORITY_NATIVE_TOOL_NAMES: tuple[str, ...] = ("media_search", "media_read")
# Root AgentDialogue turns need the finite relay/status surface to continue a
# trusted child handoff.  Keep this explicit and separate from the worker
# question tool so ordinary Agent/Chat schemas never inherit it.  The saved
# Human answer reaches the Worker only through ``answer_agent_work_question``,
# so the constrained local schema must keep it for the Root continuation turn.
ROOT_DIALOGUE_NATIVE_TOOL_NAMES: tuple[str, ...] = (
    "send_agent_work_message",
    "get_agent_work_status",
    "delegate_agent_work",
    "answer_agent_work_question",
)
# Native tool schemas are sent outside the message budget.  Above the
# constrained window they may still use at most this share of the input
# tokens; descriptions are shortened first and only then are the
# lowest-priority tools left out (deferred packs stay behind load_tool_pack).
NATIVE_TOOL_SCHEMA_INPUT_TOKEN_SHARE = 0.3
# (tool description chars, parameter description chars); None keeps the text.
NATIVE_TOOL_SCHEMA_COMPACTION_LEVELS: tuple[tuple[Optional[int], Optional[int]], ...] = (
    (None, None),
    (400, 160),
    (200, 80),
    (100, 0),
)
# A context overflow inside a tool loop is retried with each tool result
# clipped to this many characters before tools are dropped.
TOOL_RESULT_OVERFLOW_RETRY_CHARS = 2400
# Delegated task loops keep this many newest tool results whole when the
# accumulated history must be reduced to the local context window.
TOOL_LOOP_KEEP_RECENT_RESULTS = 2
# A pass over an over-budget tool loop reclaims down to this share of the input
# budget, so the next rounds fit without rewriting the (cached) prefix again.
TOOL_LOOP_RECLAIM_TARGET = 0.8
# ... and (implementation choice, not a product requirement) never above this
# share of the model's context window.
TOOL_LOOP_RECLAIM_WINDOW_SHARE = 0.65
TOOL_LOOP_CLIP_STATE_LIMIT = 4096
TOOL_LOOP_OMITTED_RESULT = (
    "[Earlier tool result omitted to fit the local context window; "
    "call the tool again if the data is needed.]"
)
TOOL_LOOP_ARGUMENT_STRING_LIMITS = (400, 120)
TOOL_LOOP_STUB_ARGUMENT_CHARS = 100
TOOL_LOOP_FINAL_ANSWER_INSTRUCTION = (
    "ツールはこれ以上使えません。ここまでに得たツール結果だけを根拠に、"
    "ユーザーへの最終回答を書いてください。ツール名や呼び出し形式は書かず、"
    "根拠が足りない点はそのように伝えてください。"
)


def _local_timing_input_stats(payload: Dict[str, Any]) -> Dict[str, int]:
    """Return bounded request-size diagnostics without exposing prompt text."""

    messages = payload.get("messages")
    tools = payload.get("tools")
    try:
        message_count = len(messages) if isinstance(messages, list) else 0
        input_chars = len(
            json.dumps(messages or [], ensure_ascii=False, separators=(",", ":"))
        )
        tool_count = len(tools) if isinstance(tools, list) else 0
    except Exception:
        return {}
    return {
        "message_count": max(0, int(message_count)),
        "input_chars": max(0, int(input_chars)),
        "tool_count": max(0, int(tool_count)),
    }


def _emit_local_timing(
    stage: str,
    phase: str,
    *,
    started_at: float | None = None,
    callback: Any = None,
    **details: Any,
) -> None:
    """Publish the shared timing contract from synchronous local generation."""

    emitter = callback if callback is not None else _LOCAL_TIMING_CALLBACK.get()
    if emitter is None:
        return
    payload: Dict[str, Any] = {
        "stage": str(stage),
        "phase": str(phase),
        "request_id": _LOCAL_TIMING_ID.get(),
    }
    if started_at is not None:
        payload["elapsed_ms"] = max(0, int(round((time.monotonic() - started_at) * 1000)))
    for key, value in details.items():
        if value is not None or key in _CATALOG_CAPTURE_NULLABLE_TIMING_KEYS:
            payload[key] = value
    try:
        emitter("llm_timing", payload)
    except Exception:
        # Timing is diagnostic-only and must never change the generation result.
        logger.debug("local timing event emission failed", exc_info=True)


def _canonical_catalog_names(values: Any) -> list[str] | None:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return None
    names: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            return None
        name = value.strip()
        if not name or len(name) > _CATALOG_CAPTURE_MAX_NAME_CHARS:
            return None
        names.add(name)
    return sorted(names)


def _display_catalog_names(names: list[str]) -> list[str]:
    return names[:_CATALOG_CAPTURE_MAX_NAMES]


def _catalog_names_digest(names: list[str]) -> str:
    canonical = json.dumps(
        names,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _catalog_names_from_native_payload(
    payload: Mapping[str, Any],
) -> tuple[bool, bool, list[str] | None]:
    if "tools" not in payload:
        return False, True, []
    tools = payload.get("tools")
    if tools is None:
        return True, False, None
    if not isinstance(tools, list):
        return True, False, None
    names: list[str] = []
    for item in tools:
        if not isinstance(item, Mapping):
            return True, False, None
        function = item.get("function")
        if not isinstance(function, Mapping):
            return True, False, None
        name = function.get("name")
        if not isinstance(name, str) or not name.strip():
            return True, False, None
        names.append(name)
    canonical = _canonical_catalog_names(names)
    return True, canonical is not None, canonical


def _catalog_hint_carrier_prefix_digest(
    messages: Any,
    hint: str,
) -> str | None:
    if not hint or not isinstance(messages, list):
        return None
    trusted_fragment = (
        f"[Current tool hints]\n{hint}\n\n"
        "[Current user input]\nCurrent user request:\n"
    )
    for message in reversed(messages):
        if not isinstance(message, Mapping) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            return None
        marker_index = content.find(trusted_fragment)
        if marker_index < 0:
            return None
        prefix = content[: marker_index + len(trusted_fragment)]
        return hashlib.sha256(prefix.encode("utf-8")).hexdigest()
    return None
LOCAL_MODEL_LOADING_RESPONSE = (
    "ローカルLLMはモデルを読み込み中です。少し待ってからもう一度送信してください。"
)
LOCAL_MODEL_SERVER_START_FAILED_RESPONSE = (
    "ローカルLLMサーバー（llama-server）を起動できませんでした。"
    "logs/models/llama_cpp.log と、選択したprofileのruntime要件を確認してから再試行してください。"
)
STRATA_MODEL_SERVER_START_FAILED_RESPONSE = (
    "ローカルStrataサーバーで応答を生成できませんでした。"
    "logs/models/strata.log と、選択したStrata runtime設定を確認してから再試行してください。"
)
LOCAL_MODEL_CONTEXT_OVERFLOW_RESPONSE = (
    "ローカルLLMのコンテキスト上限を超えたため、通常の応答生成に失敗しました。"
    "プロンプトを圧縮して再試行しましたが成功しなかったため、"
    "AoiTalk側で取得済みの短い根拠だけを表示します。"
)
MEMORY_EXTRACTION_MAX_TOKENS = 4096
# Strata Thinking can spend the ordinary local response reserve entirely in
# its reasoning channel.  This is only an upper bound for the curator's
# explicitly scoped response budget; ordinary local callers retain the
# historical 4096-token ceiling.
STRATA_THINKING_RESPONSE_TOKENS_LIMIT = 8192
# LFM2.5's published llama.cpp quickstart uses max_tokens=4096.  Its
# always-thinking output includes both the reasoning section and final text,
# so the generic 1024-token local reserve is too small when no route-specific
# cap was supplied.  ContextBudget still applies its 35%-of-context safety
# ceiling: the current profile ctx=16384 leaves the 4096-token reserve intact;
# an explicitly selected ctx=8192 is capped at 2867 tokens instead.
ALWAYS_THINKING_DEFAULT_OUTPUT_TOKENS = 4096
STRATA_MODE_SELECTION_FAILURE_RESPONSE = (
    "Strataのresponse modeを取得できないため、生成を開始できませんでした。"
    "fast または thinking を選択してから再試行してください。"
)


def _stream_generation_checkpoint() -> None:
    """Stop a local stream before publishing work from a cancelled turn.

    ``GenerationBudget`` already checks the run cancellation handle when an
    ephemeral budget is present.  Normal chat streams may have no budget,
    however, and llama.cpp can emit several reasoning-only deltas that never
    reach the outer async iterator.  Keep this cooperative fence usable from
    both the provider-delta loop and the async relay.
    """

    budget = current_generation_budget.get()
    if budget is not None:
        budget.checkpoint()
    else:
        handle = get_current_generation_cancellation()
        if handle is not None and handle.cancel_requested.is_set():
            raise GenerationCancelled("local stream generation cancelled")
    raise_if_generation_interrupted()


async def _run_owned_local_worker(operation: Callable[[], Any]) -> Any:
    """Own and drain one synchronous local generation worker."""

    handle = get_current_generation_cancellation()
    owned = None
    token = None
    if handle is None:
        owned = register_generation_cancellation(
            f"local-worker-{uuid4()}"
        )
        token = set_current_generation_cancellation(owned)
    try:
        return await run_owned_cli_worker(operation)
    finally:
        if token is not None:
            reset_current_generation_cancellation(token)
        if owned is not None:
            release_generation_cancellation(owned)


MIN_USER_TURN_CHARS = 200


class SystemInstructionsExceedContextBudget(RuntimeError):
    """Owned system instructions leave no room for the user turn."""


class StrataModeResolutionError(RuntimeError):
    """The live Strata client mode is missing or outside its wire contract."""


class LocalToolSelectionError(RuntimeError):
    """A local request cannot preserve its explicit native tool selection."""


class LocalProviderNotStartedError(ConnectionError):
    """Local readiness failed before this turn entered the SDK sender."""

    code = "agent_dialogue_provider_not_started"

    def __init__(self, *, request_type: str, run_id: str):
        super().__init__("local provider readiness failed before SDK sender")
        safe_request_type = (
            request_type
            if request_type in {"chat", "stream", "retry", "tool"}
            else "chat"
        )
        from ..services.agent_run_service import PreproviderFailureProof

        self.preprovider_proof = PreproviderFailureProof(
            run_id=str(run_id),
            proof_id=uuid4().hex,
            request_type=safe_request_type,
        )


LOCAL_MODEL_EMPTY_RESPONSE = (
    "ローカルLLMから本文のない応答が返りました。"
    "モデルのthinking設定、コンテキスト量、またはOpenAI互換サーバーの応答形式を確認してください。"
)
TITLE_GENERATION_SYSTEM_PROMPT = (
    "You generate concise Japanese chat history titles. "
    "Return only the title, without explanations, quotes, markdown, or labels."
)
REASONING_OUTPUT_FIELDS = (
    "reasoning_content",
    "reasoning",
    "thinking_content",
)


# A direct Help provider call can return a lazy stream whose consumer runs
# after the request's ContextVar scope has been reset.  Keep the ordinary
# provider ledgers while the isolated stream is in flight, then restore them
# when the stream closes.  The fields are intentionally data-only; client,
# history-manager, and privacy-gateway identity are handled separately.
_HELP_STREAM_PROVIDER_STATE_FIELDS = (
    "_last_model_transcript",
    "_last_usage",
    "_last_generation_metrics",
    "_last_context_snapshots",
    "_last_memory_selection_observation",
    "_last_tool_calls",
    "_last_agentic_events",
    "_last_tool_loop_messages",
    "_last_tool_loop_completion_confirmed",
    "_last_audit_tool_calls",
    "_current_turn_system_content",
    "_current_context_budget",
    "_current_tool_hint_context",
    "_current_tool_hint_catalog_names",
    "_current_tool_hint_carrier_prefix_sha256",
    "_current_context_bundle",
    "_current_prompt_stable_context",
    "_current_prompt_dynamic_context",
    "_current_project_cache_revision",
    "_current_dynamic_context",
    "_current_dynamic_context_metadata",
    "_last_privacy_payload",
    "_last_provider_response_diagnostic",
    "_last_provider_terminal_evidence",
    "_cache_key",
    "_privacy_project_metadata",
)
_HELP_STREAM_STATE_MISSING = object()


def _copy_help_stream_state_value(value: Any) -> Any:
    """Copy a provider ledger without making isolation depend on deepcopy."""

    if value is _HELP_STREAM_STATE_MISSING:
        return value
    try:
        return copy.deepcopy(value)
    except Exception:
        if isinstance(value, list):
            return list(value)
        if isinstance(value, dict):
            return dict(value)
        return value


def _snapshot_help_stream_provider_state(client: Any) -> dict[str, Any]:
    """Capture ordinary provider fields before an isolated Help stream."""

    snapshot: dict[str, Any] = {}
    for name in _HELP_STREAM_PROVIDER_STATE_FIELDS:
        if hasattr(client, name):
            snapshot[name] = _copy_help_stream_state_value(getattr(client, name))
        else:
            snapshot[name] = _HELP_STREAM_STATE_MISSING
    return snapshot


def _restore_help_stream_provider_state(
    client: Any,
    snapshot: dict[str, Any],
) -> None:
    """Restore ordinary provider fields, including fields absent at setup."""

    for name, value in snapshot.items():
        try:
            if value is _HELP_STREAM_STATE_MISSING:
                if hasattr(client, name):
                    delattr(client, name)
            else:
                setattr(client, name, _copy_help_stream_state_value(value))
        except Exception:
            continue


class _LifecycleStream(Iterator[str]):
    """Keep local-provider turn state alive until a returned stream closes.

    ``generate_response`` builds the ContextBundle synchronously, but the
    returned stream does not execute until its first ``next`` call.  A plain
    generator ``finally`` in the outer method therefore runs too early.  This
    small iterator owns the stream's copied Context and invokes the cleanup
    callback exactly once after normal exhaustion, an exception, or ``close``.
    """

    def __init__(
        self,
        iterator: Iterator[str],
        *,
        context: contextvars.Context,
        on_close: Any,
    ) -> None:
        self._iterator = iterator
        self._context = context
        self._on_close = on_close
        self._state_lock = threading.Lock()
        self._next_active = False
        self._close_requested = False
        self._finalizing = False
        self._closed = False

    def __iter__(self) -> "_LifecycleStream":
        return self

    def __next__(self) -> str:
        with self._state_lock:
            if self._closed or self._close_requested:
                raise StopIteration
            if self._next_active:
                raise RuntimeError("Concurrent _LifecycleStream iteration is rejected")
            self._next_active = True
        try:
            value = self._context.run(next, self._iterator)
        except BaseException:
            should_finalize = self._finish_next(close_requested=True)
            if should_finalize:
                try:
                    self._finalize()
                except BaseException:
                    # Cleanup must not replace the iterator's original error.
                    pass
            raise
        should_finalize = self._finish_next(close_requested=False)
        if should_finalize:
            self._finalize()
        return value

    def close(self) -> None:
        should_finalize = False
        with self._state_lock:
            if self._closed or self._finalizing:
                return
            self._close_requested = True
            if not self._next_active:
                self._finalizing = True
                should_finalize = True
        if should_finalize:
            self._finalize()

    def _finish_next(self, *, close_requested: bool) -> bool:
        with self._state_lock:
            self._next_active = False
            if close_requested:
                self._close_requested = True
            if self._close_requested and not self._closed and not self._finalizing:
                self._finalizing = True
                return True
            return False

    def _finalize(self) -> None:
        close_error: BaseException | None = None
        try:
            close = getattr(self._iterator, "close", None)
            if callable(close):
                self._context.run(close)
        except BaseException as exc:
            close_error = exc
        finally:
            try:
                self._context.run(self._on_close)
            except BaseException as exc:
                if close_error is None:
                    close_error = exc
            finally:
                with self._state_lock:
                    self._closed = True
                    self._finalizing = False
        if close_error is not None:
            raise close_error

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def _current_date_context() -> str:
    now = datetime.now().astimezone()
    timezone_name = now.tzname() or "local time"
    return (
        "Current date context:\n"
        f"- Today is {now.date().isoformat()} ({now.strftime('%A')}, {timezone_name}).\n"
        "- Resolve relative dates such as today, tomorrow, this week, and 今週 against this date.\n"
        "- Treat this week as Monday through Sunday unless the user specifies another range."
    )


def _deep_merge_dict(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _message_field(message: Any, field: str) -> Any:
    if isinstance(message, dict):
        return message.get(field)
    value = getattr(message, field, None)
    if value is not None:
        return value
    if hasattr(message, "model_dump"):
        try:
            dumped = message.model_dump()
            if isinstance(dumped, dict):
                return dumped.get(field)
        except Exception:
            return None
    return None


def _message_field_text(message: Any, field: str) -> str:
    value = _message_field(message, field)
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    text = str(value).strip()
    return text if text and text != "None" else ""


def _message_has_reasoning_output(message: Any) -> bool:
    return any(_message_field_text(message, field) for field in REASONING_OUTPUT_FIELDS)


def _stream_reasoning_delta_text(delta: Any, *, strip: bool = True) -> str:
    """Read explicit reasoning fields without treating content markup twice.

    ``strip=False`` keeps token-boundary whitespace so coalesced deltas join
    back into the original reasoning text.
    """

    for field in (
        "reasoning_content",
        "reasoning",
        "thinking_content",
        "thinking",
    ):
        value = _message_field(delta, field)
        if isinstance(value, str) and not strip and value:
            return value
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            joined = "\n".join(
                str(item.get("text") or "") if isinstance(item, dict) else str(item or "")
                for item in value
            ).strip()
            if joined:
                return joined
    return ""


def _config_get(config: Optional[Config], key: str, default: Any = None) -> Any:
    if isinstance(config, dict):
        value: Any = config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value
    if hasattr(config, "get"):
        return config.get(key, default)
    return default


def _normalize_base_url(base_url: str) -> str:
    clean = (base_url or DEFAULT_LOCAL_BASE_URL).rstrip("/")
    if clean.endswith("/v1"):
        return clean
    return f"{clean}/v1"


def _is_local_model_loading_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status_code is None and response is not None:
        status_code = getattr(response, "status_code", None)

    if status_code != 503:
        return False

    body = getattr(exc, "body", None)
    text = f"{exc} {body}".lower()
    return "loading model" in text or "unavailable_error" in text


def _is_context_overflow_error(exc: Exception) -> bool:
    body = getattr(exc, "body", None)
    text = f"{exc} {body}".casefold()
    overflow_terms = (
        "context size",
        "context length",
        "context window",
        "maximum context",
        "max context",
        "n_ctx",
        "too many tokens",
        "prompt is too long",
        "requested tokens",
        "exceeds context",
        "exceeded context",
        "exceeds the context",
        "exceeded the context",
        "exceeds maximum context",
        "コンテキスト",
    )
    return any(term in text for term in overflow_terms)


def _is_connection_error(exc: Exception) -> bool:
    return classify_generation_error(exc).kind == GenerationErrorKind.CONNECTION


_OPTIONAL_WIRE_KEYS = frozenset(
    {"tools", "tool_choice", "response_format", "extra_body", "functions", "function_call"}
)
_UNSUPPORTED_WIRE_MARKERS = (
    "unsupported parameter",
    "unsupported field",
    "unsupported argument",
    "unknown parameter",
    "unknown field",
    "unknown argument",
    "unrecognized parameter",
    "unrecognized field",
    "unexpected keyword",
    "unexpected argument",
    "extra inputs are not permitted",
    "does not support",
    "not supported",
    "not allowed",
)
_OPTIONAL_WIRE_FIELD_MARKERS = frozenset(
    {
        "chat_template_kwargs",
        "reasoning_effort",
        "enable_thinking",
        "reasoning",
        "top_k",
        "repeat_penalty",
    }
)


def _safe_exception_wire_text(exc: BaseException) -> str:
    """Return bounded provider contract text for classification only.

    The value is never persisted or logged.  Keep this narrow so arbitrary
    provider response bodies cannot become an observable retry reason.
    """

    parts: list[str] = [str(exc or "")]
    body = getattr(exc, "body", None)
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, Mapping):
            for key in ("code", "type", "message", "param", "parameter", "field"):
                value = error.get(key)
                if isinstance(value, (str, int, float)):
                    parts.append(str(value))
        for key in ("code", "type", "message", "param", "parameter", "field"):
            value = body.get(key)
            if isinstance(value, (str, int, float)):
                parts.append(str(value))
    return " ".join(parts).casefold()[:2000]


def _is_unsupported_optional_parameter_error(
    exc: BaseException,
    payload: Mapping[str, Any],
) -> bool:
    """Allow compatibility retry only for an explicit wire rejection.

    A post-response parser/normalization ``TypeError`` must never turn a
    successful native-tool request into a second request without tools.
    """

    optional_keys = _OPTIONAL_WIRE_KEYS.intersection(payload.keys())
    if not optional_keys:
        return False
    text = _safe_exception_wire_text(exc)
    if not any(marker in text for marker in _UNSUPPORTED_WIRE_MARKERS):
        return False
    wire_field_markers = set(optional_keys)
    if "extra_body" in optional_keys:
        wire_field_markers.update(_OPTIONAL_WIRE_FIELD_MARKERS)
    if not any(marker in text for marker in wire_field_markers):
        return False
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status in {400, 422}
    # Local OpenAI-compatible servers often raise a plain ValueError for an
    # explicitly named unsupported request field.  The marker + field gate is
    # strict enough to preserve this narrow compatibility seam.
    return isinstance(exc, (ValueError, TypeError))


def _safe_completion_response_shape(response: Any) -> dict[str, Any]:
    """Project only response shape facts; never include body/content/args."""

    def field(value: Any, name: str) -> Any:
        if isinstance(value, Mapping):
            return value.get(name)
        return getattr(value, name, None)

    choices = field(response, "choices")
    shape: dict[str, Any] = {
        "response_type": type(response).__name__,
        "choices_type": type(choices).__name__ if choices is not None else None,
        "choices_is_none": choices is None,
        "choice_count": len(choices) if isinstance(choices, (list, tuple)) else None,
        "usage_type": (
            type(field(response, "usage")).__name__
            if field(response, "usage") is not None
            else None
        ),
        "usage_is_none": field(response, "usage") is None,
        "timings_type": (
            type(field(response, "timings")).__name__
            if field(response, "timings") is not None
            else None
        ),
        "timings_is_none": field(response, "timings") is None,
    }
    if isinstance(choices, (list, tuple)) and choices:
        message = field(choices[0], "message")
        tool_calls = field(message, "tool_calls")
        content = field(message, "content")
        shape.update(
            {
                "message_type": type(message).__name__,
                "content_type": type(content).__name__ if content is not None else None,
                "content_is_none": content is None,
                "tool_calls_type": (
                    type(tool_calls).__name__ if tool_calls is not None else None
                ),
                "tool_calls_is_none": tool_calls is None,
                "tool_call_count": (
                    len(tool_calls)
                    if isinstance(tool_calls, (list, tuple))
                    else None
                ),
            }
        )
    return shape


_SAFE_PROVIDER_FINISH_REASONS = frozenset(
    {
        "stop",
        "length",
        "tool_calls",
        "function_call",
        "content_filter",
        "eos_token",
        "error",
        "cancelled",
    }
)
_SAFE_LOCAL_REQUEST_TYPES = frozenset(
    {
        "chat",
        "tool",
        "retry",
        "title",
        "plain",
        "stream",
        "review",
        "continuation",
        "memory_extraction",
        "memory_extraction_retry",
    }
)


def _safe_local_sender_attempt_ordinal() -> tuple[int, bool]:
    try:
        return max(0, int(_LOCAL_PROVIDER_SENDER_ATTEMPTS.get())), False
    except Exception:
        return 0, True


def _safe_completion_response_diagnostic(
    response: Any,
    *,
    request_type: str,
) -> dict[str, Any]:
    """Project bounded response metadata without touching response content."""

    def field(value: Any, name: str) -> Any:
        if isinstance(value, Mapping):
            return value.get(name)
        return getattr(value, name, None)

    choices = field(response, "choices")
    usage = field(response, "usage")
    timings = field(response, "timings")
    diagnostic: dict[str, Any] = {
        "response_type": type(response).__name__,
        "choices_type": type(choices).__name__ if choices is not None else None,
        "choices_is_none": choices is None,
        "choice_count": len(choices) if isinstance(choices, (list, tuple)) else None,
        "usage_present": usage is not None,
        "timings_present": timings is not None,
        "content_length": None,
        "reasoning_length": None,
        "reasoning_close_retry_effective": None,
        "reasoning_close_retry_provenance": "unknown_live_process",
        "diagnostic_failed": False,
    }
    first_choice = choices[0] if isinstance(choices, (list, tuple)) and choices else None
    raw_finish_reason = field(first_choice, "finish_reason")
    finish_reason_present = raw_finish_reason is not None
    normalized_finish_reason = (
        raw_finish_reason.casefold()
        if isinstance(raw_finish_reason, str)
        and raw_finish_reason.casefold() in _SAFE_PROVIDER_FINISH_REASONS
        else "other"
        if finish_reason_present
        else None
    )
    finish_reason_known = (
        isinstance(raw_finish_reason, str)
        and raw_finish_reason.casefold() in _SAFE_PROVIDER_FINISH_REASONS
    )
    sender_attempt_ordinal, sender_attempt_failed = (
        _safe_local_sender_attempt_ordinal()
    )
    request_kind = (
        request_type
        if isinstance(request_type, str)
        and request_type in _SAFE_LOCAL_REQUEST_TYPES
        else "unknown"
    )
    diagnostic.update(
        {
            "request_id": _LOCAL_TIMING_ID.get(),
            "request_type": request_kind,
            "sender_attempt_ordinal": sender_attempt_ordinal,
            "response_present": response is not None,
            "response_id_present": field(response, "id") is not None,
            "finish_reason": normalized_finish_reason,
            "finish_reason_present": finish_reason_present,
            "finish_reason_known": finish_reason_known,
            "diagnostic_failed": sender_attempt_failed,
        }
    )
    message = field(first_choice, "message")
    tool_calls = field(message, "tool_calls")
    diagnostic["tool_call_count"] = (
        len(tool_calls) if isinstance(tool_calls, (list, tuple)) else None
    )
    return diagnostic


def _safe_reasoning_text_length(message: Any) -> int | None:
    """Return only the length of an explicit reasoning field."""

    for field_name in REASONING_OUTPUT_FIELDS:
        value = _message_field(message, field_name)
        if isinstance(value, str):
            return len(value)
        if isinstance(value, list):
            length = 0
            for item in value:
                if isinstance(item, str):
                    length += len(item)
                elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
                    length += len(item["text"])
            if length:
                return length
    return None


def _safe_traceback_frames(exc: BaseException) -> list[dict[str, Any]]:
    """Return source coordinates only; omit locals, arguments, and messages."""

    return [
        {"file": frame.filename, "line": frame.lineno, "function": frame.name}
        for frame in traceback.extract_tb(exc.__traceback__)[-8:]
    ]


STREAM_ECHO_PROBE_LIMIT = 4096
# A drain slower than this means the stream consumer (durable audit /
# WebSocket relay) is the bottleneck; later provider deltas are coalesced.
STREAM_SLOW_CONSUMER_THRESHOLD_SECONDS = 0.05
STREAM_SLOW_CONSUMER_COALESCE_SECONDS = 0.25
_STREAM_TOOL_CALL_MARKER_RE = re.compile(
    r"^\s*(?:【\s*ツール[^】]{0,12}】|\[\s*ツール[^\]]{0,12}\]|<\s*/?\s*tool_call\s*>"
    r"|(?:tool[ _]?call|function[ _]?call|ツール(?:実行|呼び出し))\s*[:：])",
    re.IGNORECASE,
)

# Managed reasoning templates may put the thinking section in ``content``
# rather than exposing ``reasoning_content`` on every delta.  Keep this parser
# deliberately local to the stream boundary: only a leading, model-supported
# section is control markup.  A later literal ``<think>`` in prose or code is
# ordinary response text.
_STREAM_REASONING_OPEN_TAG = "<think>"
_STREAM_REASONING_CLOSE_TAG = "</think>"
_STREAM_REASONING_PROBE_LIMIT = 64


_ASCII_LOWER = {ord(c): ord(c) + 32 for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"}


def _ascii_fold(text: str) -> str:
    """Lowercase ASCII only, so indexes stay valid for the original string."""

    return str(text or "").translate(_ASCII_LOWER)


def _longest_stream_reasoning_close_prefix(text: str) -> int:
    """Return the suffix length that may begin a split ``</think>`` tag."""

    folded = _ascii_fold(text)
    close = _STREAM_REASONING_CLOSE_TAG
    for size in range(min(len(folded), len(close) - 1), 0, -1):
        if folded.endswith(close[:size]):
            return size
    return 0


class _LeadingThinkStreamParser:
    """Strip only a leading reasoning block from a supported model stream.

    The parser never accumulates the final answer.  While probing it keeps at
    most a short tag-sized prefix (plus bounded whitespace); while in the
    reasoning section it keeps only the suffix that could be a split closing
    tag.  This keeps ordinary final text chunked and leaves cancellation and
    provider cleanup to the existing stream loop.
    """

    def __init__(self, enabled: bool) -> None:
        self._phase = "probe" if enabled else "disabled"
        self._probe = ""
        self._reasoning_tail = ""
        self._strip_final_lead = False
        self._allow_open_tag = True
        self.saw_reasoning = False
        self.unterminated_reasoning = False

    def _final_parts(self, text: str) -> list[str]:
        if self._strip_final_lead:
            text = text.lstrip()
            if text:
                self._strip_final_lead = False
        return [text] if text else []

    def enable(self) -> None:
        """Enable markup parsing before the first content delta, if needed."""

        if self._phase == "disabled":
            self._phase = "probe"
            # An explicit reasoning field already separated the thinking, so
            # a leading literal ``<think>`` in content is answer text.
            self._allow_open_tag = False

    def _feed_probe(self, text: str) -> tuple[list[str], list[str]]:
        combined = self._probe + str(text or "")
        if not combined:
            return [], []
        first_content = next(
            (index for index, char in enumerate(combined) if not char.isspace()),
            len(combined),
        )
        if first_content == len(combined):
            # Whitespace can precede a split opening tag.  Flush older
            # whitespace so an adversarial provider cannot grow this probe.
            if len(combined) > _STREAM_REASONING_PROBE_LIMIT:
                keep = len(_STREAM_REASONING_OPEN_TAG) - 1
                flush = combined[:-keep]
                self._probe = combined[-keep:]
                return [], [flush]
            self._probe = combined
            return [], []

        candidate = combined[first_content:]
        folded_candidate = _ascii_fold(candidate)
        open_tag = _STREAM_REASONING_OPEN_TAG
        close_tag = _STREAM_REASONING_CLOSE_TAG
        if self._allow_open_tag and folded_candidate.startswith(open_tag):
            self._phase = "thinking"
            self.saw_reasoning = True
            self._probe = ""
            return self._feed_thinking(combined[first_content + len(open_tag) :])
        if folded_candidate.startswith(close_tag):
            # Some OpenAI-compatible servers omit the opening tag in content
            # after exposing reasoning on a separate delta.
            self._phase = "final"
            self._probe = ""
            self._strip_final_lead = True
            return [], self._final_parts(combined[first_content + len(close_tag) :])
        if (
            self._allow_open_tag and open_tag.startswith(folded_candidate)
        ) or close_tag.startswith(folded_candidate):
            # Hold only an incomplete control-tag prefix.  ``candidate`` is
            # necessarily shorter than one of the fixed tags here.
            self._probe = combined
            return [], []

        self._phase = "final"
        self._probe = ""
        return [], [combined]

    def _feed_thinking(self, text: str) -> tuple[list[str], list[str]]:
        combined = self._reasoning_tail + str(text or "")
        if not combined:
            return [], []
        close_tag = _STREAM_REASONING_CLOSE_TAG
        close_index = _ascii_fold(combined).find(close_tag)
        if close_index >= 0:
            reasoning = combined[:close_index]
            remainder = combined[close_index + len(close_tag) :]
            self._reasoning_tail = ""
            self._phase = "final"
            self._strip_final_lead = True
            return ([reasoning] if reasoning else []), self._final_parts(
                remainder
            )
        suffix_length = _longest_stream_reasoning_close_prefix(combined)
        if suffix_length:
            reasoning = combined[:-suffix_length]
            self._reasoning_tail = combined[-suffix_length:]
        else:
            reasoning = combined
            self._reasoning_tail = ""
        return ([reasoning] if reasoning else []), []

    def feed(self, text: str) -> tuple[list[str], list[str]]:
        if not text:
            return [], []
        if self._phase == "disabled":
            self._phase = "final"
            return [], [str(text)]
        if self._phase == "probe":
            return self._feed_probe(str(text))
        if self._phase == "thinking":
            return self._feed_thinking(str(text))
        return [], self._final_parts(str(text))

    def finish(self) -> tuple[list[str], list[str]]:
        if self._phase == "probe":
            probe = self._probe
            self._probe = ""
            self._phase = "final"
            return [], [probe] if probe else []
        if self._phase == "thinking":
            reasoning = self._reasoning_tail
            self._reasoning_tail = ""
            self.unterminated_reasoning = True
            self._phase = "done"
            return [reasoning] if reasoning else [], []
        self._probe = ""
        self._reasoning_tail = ""
        self._phase = "done"
        return [], []


def _stream_incomplete_marker_call_echo(text: str, tool_names: Any = ()) -> bool:
    """Recognize a marker/name/open-args echo that lacks its closing delimiter."""

    value = _strip_leading_think_markup(str(text or "")).strip()
    marker_match = _STREAM_TOOL_CALL_MARKER_RE.match(value)
    if not marker_match:
        return False
    names = {str(tool_name) for tool_name in tool_names or () if tool_name}
    remainder = value[marker_match.end() :].lstrip()
    marker_name = re.match(
        r"`?([A-Za-z_][A-Za-z0-9_.-]*)`?\s*(.*)$",
        remainder,
        flags=re.DOTALL,
    )
    if not marker_name:
        return False
    name, tail = marker_name.groups()
    if name not in names and not (not names and "_" in name):
        return False
    tail = tail.lstrip()
    if not tail or tail[:1] not in "({[":
        return False
    return _has_incomplete_call_suffix(tail)


def _stream_echo_probe_needed(text: str, tool_names: Any = ()) -> bool:
    """Return whether only a suspicious leading fragment needs buffering."""

    value = _strip_leading_think_markup(str(text or "")).lstrip()
    if not value:
        return False
    names = {str(tool_name) for tool_name in tool_names or () if tool_name}
    if value[:1] in "<{[":
        return True
    marker_prefixes = (
        "【ツール実行】",
        "【ツール呼び出し】",
        "[ツール実行]",
        "[ツール呼び出し]",
        "tool call:",
        "tool_call:",
        "function call:",
        "function_call:",
        "ツール実行:",
        "ツール呼び出し:",
    )
    folded = value.casefold()
    if any(prefix.casefold().startswith(folded) for prefix in marker_prefixes):
        return True
    marker_match = _STREAM_TOOL_CALL_MARKER_RE.match(value)
    if marker_match:
        remainder = value[marker_match.end() :].lstrip()
        if not remainder:
            # A complete marker can still be followed by the tool name in the
            # next provider chunk.  Hold it until that decision is possible.
            return True
        if remainder[:1] in "{[":
            return True
        marker_name = re.match(
            r"`?([A-Za-z_][A-Za-z0-9_.-]*)`?\s*(.*)$",
            remainder,
            flags=re.DOTALL,
        )
        if not marker_name:
            return False
        name, tail = marker_name.groups()
        allowed = name in names or (not names and "_" in name)
        if not allowed:
            # An identifier may be split across chunks.  Buffer only when it
            # remains a prefix of a registered name; marker prose flushes.
            return not tail.strip() and any(
                candidate.startswith(name) and candidate != name
                for candidate in names
            )
        tail = tail.lstrip()
        if not tail:
            return True
        # Registered-name arguments may still be incomplete.  The strict
        # detector decides suppression only after the complete text arrives.
        return tail[:1] in "({["
    match = re.match(
        r"`?([A-Za-z_][A-Za-z0-9_.-]*)`?\s*(.*)$",
        value,
        flags=re.DOTALL,
    )
    if not match:
        return False
    name, rest = match.groups()
    allowed = name in names or (not names and "_" in name)
    if allowed:
        return not rest.strip() or rest.lstrip()[:1] in "({["
    if not rest.strip() and any(
        candidate.startswith(name) and candidate != name for candidate in names
    ):
        return True
    return False


def _stream_echo_call_shaped(text: str, tool_names: Any = ()) -> bool:
    """Return true when a buffered stream is recognizably a tool call.

    A bare registered name is intentionally excluded: ``media_search`` can be
    ordinary prose when the following chunk says 「で検索した結果」.
    """

    value = _strip_leading_think_markup(str(text or "")).strip()
    names = {str(tool_name) for tool_name in tool_names or () if tool_name}
    if not value or value in names:
        return False
    if looks_like_tool_call_echo(value, names):
        return True
    if _stream_incomplete_marker_call_echo(value, names):
        return True
    # Below the probe limit, an incomplete call is still undecided text.  The
    # final stream flush must not confuse it with a completed invocation echo;
    # the long-probe path is retained for providers that never emit a closing
    # delimiter and would otherwise keep buffering forever.
    if len(value) < STREAM_ECHO_PROBE_LIMIT:
        return False
    match = re.match(
        r"^`?([A-Za-z_][A-Za-z0-9_.-]*)`?\s*(.*)$",
        value,
        flags=re.DOTALL,
    )
    if match and match.group(1) in names:
        return match.group(2).lstrip()[:1] in "({["
    if value[:1] == "{" and names:
        name_match = re.search(
            r"[\"']name[\"']\s*:\s*[\"']?([A-Za-z_][A-Za-z0-9_.-]*)",
            value,
        )
        return bool(name_match and name_match.group(1) in names)
    return False


def _stream_request_needs_local_retrieval(text: str) -> bool:
    """Recognize requests whose streamed claims need retrieval protection."""

    value = str(text or "").casefold()
    if not value.strip():
        return False

    # English markers must be whole words.  In particular, ``immediately``
    # must not match ``media`` and ``searchlight`` must not match ``search``.
    english_retrieval_action = re.search(
        r"(?<![a-z0-9_])(?:search|lookup|look\s+up|retrieve|retrieval|query|find)(?![a-z0-9_])",
        value,
    )
    japanese_retrieval_action = re.search(
        r"(?:調べ(?!方)|検索|探し|探す|見つけ|取得|参照)",
        value,
    )
    if english_retrieval_action or japanese_retrieval_action:
        return True

    # ``確認``/``check`` alone is a normal writing request.  Treat it as a
    # retrieval only when it names a local source, past conversation, file,
    # document, note, or other concrete material to inspect.
    source_context = bool(
        re.search(
            r"(?<![a-z0-9_])(?:media|docs?|knowledge|documents?|conversations?|"
            r"chats?|notes?|files?|records?|history)(?![a-z0-9_])",
            value,
        )
        or re.search(
            r"(?:過去|会話|チャット|資料|原本|文書|ドキュメント|ファイル|履歴|記録|"
            r"ログ|ノート|メモ|知識)",
            value,
        )
    )
    if not source_context:
        return False
    return bool(
        re.search(
            r"(?<![a-z0-9_])(?:check|review|read|inspect|look|consult)(?![a-z0-9_])",
            value,
        )
        or re.search(r"(?:確認|読ん|読む|見て|見せて|調べ)", value)
    )


def estimate_prompt_tokens(text: str) -> int:
    """Conservative token estimate for ASCII-heavy JSON mixed with Japanese."""
    value = str(text or "")
    ascii_chars = sum(1 for char in value if ord(char) < 128)
    return int(math.ceil(ascii_chars / 3.0 + (len(value) - ascii_chars) / 1.2))


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _has_tool_result_messages(messages: Any) -> bool:
    return isinstance(messages, list) and any(
        isinstance(message, dict) and message.get("role") == "tool"
        for message in messages
    )


def _shorten_schema_text(text: str, limit: int) -> str:
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 1)].rstrip() + "…"


def _compact_parameter_schema(schema: Any, limit: int) -> None:
    """Shorten (or drop at 0) every description inside one JSON schema."""
    if isinstance(schema, list):
        for item in schema:
            _compact_parameter_schema(item, limit)
        return
    if not isinstance(schema, dict):
        return
    description = schema.get("description")
    if isinstance(description, str):
        if limit <= 0:
            schema.pop("description", None)
        else:
            schema["description"] = _shorten_schema_text(description, limit)
    schema.pop("examples", None)
    for key in ("properties", "patternProperties", "$defs", "definitions"):
        children = schema.get(key)
        if isinstance(children, dict):
            for child in children.values():
                _compact_parameter_schema(child, limit)
    for key in ("items", "additionalProperties", "not", "anyOf", "oneOf", "allOf", "prefixItems"):
        _compact_parameter_schema(schema.get(key), limit)


def compact_native_tool_spec(
    spec: Dict[str, Any],
    description_chars: Optional[int],
    parameter_chars: Optional[int],
) -> Dict[str, Any]:
    """Return an OpenAI tool spec with shortened descriptions (names/types kept)."""
    if description_chars is None and parameter_chars is None:
        return spec
    compacted = copy.deepcopy(spec)
    function = compacted.get("function")
    if not isinstance(function, dict):
        return compacted
    description = function.get("description")
    if description_chars is not None and isinstance(description, str):
        function["description"] = _shorten_schema_text(description, description_chars)
    if parameter_chars is not None:
        _compact_parameter_schema(function.get("parameters"), parameter_chars)
    return compacted


# 予算で一部のスキーマを落とす時、Agent scope内ツールをアルファベット順に
# 消費すると、後ろの search_files が先に落ちる。作業の観測→編集に要る
# ツールを先に保持し、残りは名前順を保つ。
WORK_LOOP_NATIVE_TOOL_ORDER: tuple[str, ...] = (
    "read_file",
    "edit_file",
    "search_files",
    "create_file",
    "list_directory",
    "execute_command",
)


def _ordered_scope_priority(hinted: set[str], scope_names: set[str]) -> List[str]:
    """Priority list: hinted tools, then the work-loop tools the scope allows."""
    names = hinted | scope_names
    first = sorted(hinted)
    work = [name for name in WORK_LOOP_NATIVE_TOOL_ORDER if name in scope_names]
    rest = sorted(names - set(first) - set(work))
    return list(dict.fromkeys([*first, *work, *rest]))


def _native_tool_keep_order(tools: List[Any], priority_names: List[str]) -> List[int]:
    """Indices in the order tools are kept under a schema budget.

    Named priorities come first.  The rest take turns per owner domain (read-only
    tools before mutating ones inside a domain), so one large domain cannot
    crowd out every other capability.
    """
    rank = {name: index for index, name in enumerate(priority_names)}
    prioritized = sorted(
        (index for index, tool_def in enumerate(tools)
         if str(getattr(tool_def, "name", "") or "") in rank),
        key=lambda index: rank[str(getattr(tools[index], "name", "") or "")],
    )
    domains: dict[str, list[int]] = {}
    for index, tool_def in enumerate(tools):
        if index in prioritized:
            continue
        domains.setdefault(str(getattr(tool_def, "owner", "") or ""), []).append(index)
    queues = [
        sorted(
            indexes,
            key=lambda index: str(getattr(tools[index], "side_effect", "none") or "none") != "none",
        )
        for indexes in domains.values()
    ]
    interleaved: list[int] = []
    while any(queues):
        for queue in queues:
            if queue:
                interleaved.append(queue.pop(0))
    return [*prioritized, *interleaved]


def _clip_json_string(value: str, limit: int) -> str:
    """Clip one JSON string without adding an invalid raw suffix."""

    text = str(value or "")
    if limit <= 0 or len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return text[: limit - 1].rstrip() + "…"


def _compact_generic_json_value(
    value: Any,
    *,
    string_limit: int,
    list_limit: int,
    depth: int = 0,
) -> Any:
    """Reduce arbitrary JSON while retaining a valid object/array shape."""

    if isinstance(value, str):
        return _clip_json_string(value, string_limit)
    if isinstance(value, list):
        values = value[: max(1, list_limit)]
        return [
            _compact_generic_json_value(
                item,
                string_limit=string_limit,
                list_limit=max(1, list_limit // 2),
                depth=depth + 1,
            )
            for item in values
        ]
    if isinstance(value, dict):
        return {
            key: _compact_generic_json_value(
                item,
                string_limit=string_limit,
                list_limit=max(1, list_limit // 2),
                depth=depth + 1,
            )
            for key, item in value.items()
        }
    return value


def _compact_generic_json_payload(payload: Any, max_chars: int) -> str:
    """Serialize generic tool JSON after bounded, structure-preserving clipping."""

    if max_chars <= 0:
        return _compact_json(payload)
    # Keep keys and scalar values useful first.  If a tool returns a large
    # collection, progressively reduce the collection and its string fields;
    # every candidate remains valid JSON.
    candidates = (
        (max(32, max_chars // 4), 16),
        (max(24, max_chars // 8), 8),
        (max(16, max_chars // 16), 4),
        (max(8, max_chars // 32), 2),
        (4, 1),
    )
    last = _compact_json(payload)
    for string_limit, list_limit in candidates:
        compacted = _compact_generic_json_value(
            payload,
            string_limit=string_limit,
            list_limit=list_limit,
        )
        last = _compact_json(compacted)
        if len(last) <= max_chars:
            return last
    return last


def _shorten_tool_call_arguments(
    arguments: str, string_limit: int = TOOL_LOOP_ARGUMENT_STRING_LIMITS[0]
) -> str:
    """Shorten long string values in call arguments while keeping valid JSON."""

    if len(arguments) <= string_limit * 2:
        return arguments
    try:
        payload = json.loads(arguments)
    except (TypeError, ValueError):
        return arguments

    def shorten(value: Any) -> Any:
        if isinstance(value, str) and len(value) > string_limit:
            return value[:string_limit] + f"...[{len(value) - string_limit} chars omitted]"
        if isinstance(value, dict):
            return {key: shorten(item) for key, item in value.items()}
        if isinstance(value, list):
            return [shorten(item) for item in value]
        return value

    shortened = json.dumps(shorten(payload), ensure_ascii=False)
    return shortened if len(shortened) < len(arguments) else arguments


_SENSITIVE_ARGUMENT_KEY = re.compile(
    r"(?i)(token|secret|passw|credential|authorization|cookie|api[_-]?key|private[_-]?key|session)"
)
_SECRET_LIKE_VALUE = re.compile(
    r"(?i)(bearer\s+\S+|sk-[A-Za-z0-9_-]{8,}|eyJ[A-Za-z0-9_-]{10,}|[A-Za-z0-9+/_=-]{32,})"
)


def _safe_call_summary(raw_arguments: Any) -> str:
    """Summarize call arguments for an omitted-history note without leaking secrets.

    Only short scalar values of non-sensitive keys are shown; anything that
    looks like a credential, and every value of a sensitive key, is replaced.
    Unparseable arguments are not echoed at all.
    """

    try:
        payload = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    parts: list[str] = []
    for key, value in payload.items():
        key = str(key)
        if _SENSITIVE_ARGUMENT_KEY.search(key):
            parts.append(f"{key}=[redacted]")
        elif isinstance(value, (int, float, bool)) or value is None:
            parts.append(f"{key}={value}")
        elif isinstance(value, str):
            text = " ".join(value.split())[:60]
            parts.append(f"{key}={'[redacted]' if _SECRET_LIKE_VALUE.search(text) else text}")
        else:
            parts.append(f"{key}=...")
    return ", ".join(parts)[:TOOL_LOOP_STUB_ARGUMENT_CHARS]


def _collapse_oldest_tool_loop_groups(
    api_kwargs: Dict[str, Any],
    fits: Callable[[Dict[str, Any]], bool],
    *,
    protected_calls: int,
) -> Dict[str, Any]:
    """Fold the oldest call/result groups into one digest message.

    A group is an assistant message with ``tool_calls`` and the ``tool`` messages
    answering it; removing a whole group keeps the wire pairing valid.  Only the
    group indexes are removed: any system/user/assistant message between groups
    stays in place.  The digest is a neutral call history (outcomes are not
    claimed) and does not forbid fetching the data again.
    """

    messages = list(api_kwargs.get("messages") or [])
    groups: list[tuple[int, int]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if (
            isinstance(message, dict)
            and message.get("role") == "assistant"
            and isinstance(message.get("tool_calls"), list)
            and message["tool_calls"]
        ):
            end = index + 1
            while (
                end < len(messages)
                and isinstance(messages[end], dict)
                and messages[end].get("role") == "tool"
            ):
                end += 1
            groups.append((index, end))
            index = end
        else:
            index += 1
    foldable = groups[: max(0, len(groups) - max(1, protected_calls))]
    candidate = api_kwargs
    for count in range(1, len(foldable) + 1):
        folded = foldable[:count]
        lines = []
        for start, _end in folded:
            for call in messages[start]["tool_calls"]:
                function = _message_field(call, "function")
                name = str(_message_field(function, "name") or "tool") if function is not None else "tool"
                raw = _message_field(function, "arguments") if function is not None else ""
                lines.append(f"- {name}({_safe_call_summary(raw)})")
        digest = {
            "role": "assistant",
            "content": (
                "[Earlier tool call history omitted to fit the local context window "
                "(outcomes not shown; call a tool again if you still need its data):\n"
                + "\n".join(lines)
                + "]"
            ),
        }
        removed = {i for start, end in folded for i in range(start, end)}
        first_start = folded[0][0]
        rebuilt: list[Any] = []
        for i, message in enumerate(messages):
            if i == first_start:
                rebuilt.append(digest)
            if i not in removed:
                rebuilt.append(message)
        candidate = {**api_kwargs, "messages": rebuilt}
        if fits(candidate):
            break
    return candidate


def _omitted_tool_result_stub(messages: list[Any], index: int) -> str:
    """Name an omitted result's call without claiming its outcome."""

    message = messages[index]
    call_id = str(message.get("tool_call_id") or "").strip() if isinstance(message, dict) else ""
    name = _tool_name_from_message(messages, index)
    arguments = ""
    for previous in reversed(messages[:index]):
        if not isinstance(previous, dict) or previous.get("role") != "assistant":
            continue
        for call in previous.get("tool_calls") or []:
            if str(_message_field(call, "id") or _message_field(call, "tool_call_id") or "") != call_id:
                continue
            function = _message_field(call, "function")
            raw = _message_field(function, "arguments") if function is not None else ""
            arguments = _safe_call_summary(raw)
            break
        if arguments:
            break
    if not name and not arguments:
        return TOOL_LOOP_OMITTED_RESULT
    return (
        f"[Earlier {name or 'tool'} result ({arguments}) omitted to fit the local context window "
        "(outcome not shown). Call it again, with a narrower range if possible, if you still need the data.]"
    )


def _compact_tool_result_content(
    tool_name: str,
    content: str,
    max_chars: int,
) -> str:
    """Clip a tool result without cutting structured retrieval contracts."""

    raw = str(content or "")
    if max_chars <= 0 or len(raw) <= max_chars:
        return raw
    media = compact_media_tool_result(
        tool_name=tool_name,
        output=raw,
        max_chars=max_chars,
    )
    if media is not None:
        return media.text
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return clip_text(raw, max_chars)
    return _compact_generic_json_payload(payload, max_chars)


def _tool_name_from_message(messages: list[Any], index: int) -> str:
    """Resolve a tool result's name from ``name`` or its assistant call id."""

    message = messages[index] if 0 <= index < len(messages) else None
    if isinstance(message, dict):
        direct = str(message.get("name") or "").strip()
        if direct:
            return direct
        call_id = str(message.get("tool_call_id") or "").strip()
    else:
        direct = str(getattr(message, "name", "") or "").strip()
        if direct:
            return direct
        call_id = str(getattr(message, "tool_call_id", "") or "").strip()
    if not call_id:
        return ""
    for previous in reversed(messages[:index]):
        if not isinstance(previous, dict) or previous.get("role") != "assistant":
            continue
        for call in previous.get("tool_calls") or []:
            call_identifier = str(
                _message_field(call, "id")
                or _message_field(call, "tool_call_id")
                or ""
            ).strip()
            if call_identifier != call_id:
                continue
            function = _message_field(call, "function")
            return str(_message_field(function, "name") or "").strip()
    return ""


def _tool_record_parts(record: Any) -> tuple[str, dict[str, Any], str, bool]:
    """Read a tool ledger entry from either its dataclass or dict form."""

    if isinstance(record, dict):
        name = str(record.get("tool") or record.get("name") or "tool")
        arguments = record.get("arguments") or {}
        result = str(record.get("result") or record.get("output") or "")
        explicit_success = record.get("successful", record.get("success"))
    else:
        name = str(getattr(record, "tool", "") or getattr(record, "name", "") or "tool")
        arguments = getattr(record, "arguments", {}) or {}
        result = str(getattr(record, "result", "") or getattr(record, "output", "") or "")
        explicit_success = getattr(record, "successful", None)
        if explicit_success is None:
            explicit_success = getattr(record, "success", None)
    if not isinstance(arguments, dict):
        arguments = {"value": str(arguments)}
    if isinstance(explicit_success, bool):
        successful = explicit_success
    else:
        successful = True
        try:
            payload = json.loads(result)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict) and payload.get("success") is False:
            successful = False
        lowered = result.strip().casefold()
        if lowered.startswith(("error:", "tool not found:", "tool execution error:")):
            successful = False
    return name, arguments, result, successful


def _tool_record_retry_evidence(record: Any, max_chars: int = 1600) -> str:
    """Render one tool ledger entry for the model's minimal retry prompt."""

    name, arguments, result, successful = _tool_record_parts(record)
    try:
        arguments_text = _compact_json(arguments)
    except Exception:  # pragma: no cover - defensive for custom mappings
        arguments_text = str(arguments)
    arguments_text = _clip_json_string(arguments_text, 500)
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        payload = None

    media = compact_media_tool_result(
        tool_name=name,
        output=result,
        max_chars=max_chars,
    )
    media_kind = ""
    if media is not None:
        strategy = str(media.strategy or "")
        if strategy.startswith("media_search_"):
            media_kind = "media_search"
        elif strategy.startswith("media_read_"):
            media_kind = "media_read"
    if media_kind == "media_search":
        return (
            f"- media_search search candidates (not confirmed original) "
            f"({arguments_text}): {media.text}"
        )
    if media_kind == "media_read":
        evidence_label = (
            "confirmed original evidence"
            if successful and isinstance(payload, dict) and payload.get("success") is True
            else "original could not be confirmed"
        )
        return f"- media_read {evidence_label} ({arguments_text}): {media.text}"
    if isinstance(payload, (dict, list)):
        output = _compact_generic_json_payload(payload, max_chars)
    else:
        output = clip_text(result, max_chars)
    status = "successful output" if successful else "failed output"
    return f"- {name} {status} ({arguments_text}): {output}"


def _safe_public_reference(value: Any) -> str:
    """Keep only a user-facing relative or absolute URL from a tool payload."""

    reference = str(value or "").strip()
    if reference.startswith(("https://", "http://", "/")):
        return reference
    return ""


def _user_visible_tool_evidence(records: Any, max_chars: int = 600) -> list[str]:
    """Return concise evidence suitable for an overflow response to the user."""

    original_blocks: list[str] = []
    candidate_blocks: list[str] = []
    generic_success = False
    for record in list(records or [])[-6:]:
        name, _arguments, result, successful = _tool_record_parts(record)
        if not successful:
            continue
        try:
            payload = json.loads(result)
        except (TypeError, ValueError):
            payload = None
        if name == "media_read" and isinstance(payload, dict):
            if payload.get("success") is not True:
                continue
            body = str(payload.get("text") or "").strip()
            if body:
                title = str(payload.get("title") or "").strip()
                reference = _safe_public_reference(
                    payload.get("original_url") or payload.get("conversation_url")
                )
                heading = "確認できた原本"
                if title:
                    heading += f"（{_clip_json_string(title, 160)}）"
                block = f"{heading}:\n{clip_text(body, max_chars)}"
                if reference:
                    block += f"\n参照: {reference}"
                original_blocks.append(block)
            else:
                original_blocks.append(
                    "原本の取得結果はありましたが、表示できる本文がありませんでした。"
                )
            continue
        if name == "media_search" and isinstance(payload, dict):
            for item in list(payload.get("results") or [])[:3]:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "").strip()
                if title:
                    candidate_blocks.append(
                        f"候補「{_clip_json_string(title, 180)}」は見つかりましたが、原本は未確認です。"
                    )
            continue
        if successful:
            generic_success = True

    if original_blocks:
        return original_blocks
    if candidate_blocks:
        return candidate_blocks
    if generic_success:
        return [
            "取得済みの情報はありますが、原本として確認できる本文はありませんでした。"
        ]
    return []


def _as_plain_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if value is None:
        return {}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
            return dict(dumped) if isinstance(dumped, dict) else {}
        except Exception:
            pass
    result: Dict[str, Any] = {}
    for attr in ("timings", "usage", "model", "id"):
        if hasattr(value, attr):
            result[attr] = getattr(value, attr)
    extra = getattr(value, "model_extra", None)
    if isinstance(extra, dict):
        result.update(extra)
    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, dict):
        result.update(
            {key: val for key, val in attrs.items() if not key.startswith("_")}
        )
    return result


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _as_finite_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _as_non_negative_int(value: Any) -> Optional[int]:
    number = _as_finite_float(value)
    if number is None or number < 0:
        return None
    return int(number)


def _llama_cpp_profile_capability(
    profile: Optional[Dict[str, Any]],
    capability: str,
) -> Optional[bool]:
    """Return an explicitly declared capability from a runtime profile.

    ``None`` means the profile is unknown or does not declare the capability;
    callers must preserve the generic external local-model behaviour in that
    case.  Only an explicit ``False`` disables a legacy opt-in feature.
    """

    capabilities = llama_cpp_profile_capabilities(profile=profile)
    if not isinstance(capabilities, dict) or capability not in capabilities:
        return None
    return bool(capabilities.get(capability))


MIN_WIRE_COMPLETION_TOKENS = 128


class OpenAICompatibleLocalClient:
    """Client for generic local OpenAI-compatible chat completion servers."""

    # generate_response_async relays provider chunks through stream_callback.
    # ProviderCapabilities.supports_stream alone does not promise that UI contract.
    supports_content_streaming = True

    def __init__(
        self,
        base_url: str = DEFAULT_LOCAL_BASE_URL,
        model: str = DEFAULT_LOCAL_MODEL,
        api_key: str = DEFAULT_LOCAL_API_KEY,
        config: Optional[Config] = None,
        *,
        enable_tools: bool = False,
        enable_response_format: bool = False,
        enable_extra_body: bool = False,
        extra_body: Optional[Dict[str, Any]] = None,
    ):
        self.config = config
        self._lightweight_ephemeral_client = bool(
            _config_get(config, "runtime.lightweight_ephemeral_client", False)
        )
        self._privacy_gateway = OutboundPrivacyGateway(config)
        self.base_url = _normalize_base_url(base_url)
        self.provider_label = "openai_compatible_local"
        self.model_name = model or DEFAULT_LOCAL_MODEL
        self.api_key = api_key or DEFAULT_LOCAL_API_KEY
        self.client = OpenAI(base_url=self.base_url, api_key=self.api_key)
        self._managed_local_runtime = managed_local_runtime_for_model(
            config,
            self.model_name,
        )
        self._llama_cpp_profile = (
            llama_cpp_model_profile(self.model_name)
            if self._managed_local_runtime != "freetoken"
            else None
        )
        # Some managed Jinja templates always open a reasoning block.  Keep
        # that contract on the client so a persisted generic fast/thinking
        # value cannot turn into a non-thinking request or retry.
        self._profile_thinking_mode = llama_cpp_thinking_mode(
            profile=self._llama_cpp_profile
        )
        profile_tools = _llama_cpp_profile_capability(
            self._llama_cpp_profile,
            "tools",
        )
        profile_reasoning = _llama_cpp_profile_capability(
            self._llama_cpp_profile,
            "reasoning",
        )
        # A profile-declared limitation wins over the provider's opt-in
        # setting.  Unknown profiles (including ``local-model``) keep the
        # historical config-controlled behaviour.
        self.enable_tools = (
            bool(enable_tools)
            and profile_tools is not False
            and not self._lightweight_ephemeral_client
        )
        self.enable_response_format = bool(enable_response_format)
        self.enable_extra_body = bool(enable_extra_body)
        self.extra_body = extra_body if isinstance(extra_body, dict) else {}
        self.server_profile = openai_compatible_server_profile(
            config, base_url=self.base_url
        )
        self.capabilities = ProviderCapabilities(
            supports_stream=True,
            supports_tools=(
                profile_tools is not False
                and not self._lightweight_ephemeral_client
            ),
            supports_response_format=True,
            supports_model_pull=False,
            supports_model_delete=False,
            supports_extra_body=True,
        )

        if self._lightweight_ephemeral_client:
            self.character_name = "Assistant"
        elif hasattr(config, "default_character"):
            self.character_name = config.default_character
        elif isinstance(config, dict):
            self.character_name = config.get("default_character", "Assistant")
        else:
            self.character_name = "Assistant"

        self.history_manager = HistoryManager()
        if (
            not self._lightweight_ephemeral_client
            and config
            and _config_get(config, "use_tools", True)
        ):
            self._tool_registry = build_runtime_tool_registry_for_client(
                build_runtime_tool_registry,
                config,
                client=self,
            )
            ensure_load_tool_pack_tool(self._tool_registry, self)
        else:
            self._tool_registry = ToolRegistry()

        self.session_user_id = "default_user"
        self.session_metadata: Dict[str, Any] = {}
        self._privacy_session_context: Dict[str, Any] = {}
        self._privacy_project_metadata: Dict[str, Any] = {}
        self._current_session_id: Optional[str] = None
        self._history_session_id: Optional[str] = None
        self.current_project_id: Optional[str] = None
        self.current_include_project_context: Optional[bool] = None
        self.generation_policy = DEFAULT_GENERATION_POLICY
        self.current_edit_message_id: Optional[str] = None
        self._current_context_bundle: Optional[ContextBundle] = None
        self._current_prompt_stable_context: list[tuple[str, str]] = []
        self._current_prompt_dynamic_context: list[tuple[str, str]] = []
        self._current_project_cache_revision: str = ""
        self._current_context_budget: Optional[ContextBudget] = None
        self._context_window_override_tokens: Optional[int] = None
        self._current_tool_hint_context = ""
        self._current_tool_hint_catalog_names: tuple[str, ...] = ()
        self._current_tool_hint_carrier_prefix_sha256: str | None = None
        self._last_generation_metrics: Optional[Dict[str, Any]] = None
        self._last_context_snapshots: List[Dict[str, Any]] = []
        self._last_memory_selection_observation: Optional[Dict[str, Any]] = None
        self._last_tool_calls: List[Any] = []
        self._last_agentic_events: List[Dict[str, Any]] = []
        # Provider failures are retained separately from the historical
        # friendly fallback text so AgentRun/Work settlement never infers
        # success from that text.
        self._last_generation_failure = None
        self._last_model_transcript: List[Dict[str, Any]] = []
        self._last_usage: Dict[str, Any] = {}
        self._last_provider_response_shape: Dict[str, Any] | None = None
        self._last_provider_response_diagnostic: Dict[str, Any] | None = None
        self._request_usage_records: list[Dict[str, Any]] = []
        self._request_usage_unknown = False
        self._request_usage_sequence = 0
        self._last_tool_loop_messages: List[Dict[str, Any]] = []
        self._last_tool_loop_completion_confirmed = False
        self._last_audit_tool_calls: List[Any] = []
        self._current_turn_system_content = ""
        self._last_local_server_ensure_error: Optional[str] = None
        self._llama_cpp_generation_lease_tickets: set[Any] = set()
        self._llama_cpp_runtime_consumer_lease_tickets: set[Any] = set()
        self._llama_cpp_runtime_consumer_lease_lock = threading.RLock()
        self._local_runtime_consumer_lease_retired = False
        self._local_runtime_consumer_lease_retire_release_complete = False
        self._local_runtime_consumer_lease_release_requested = False
        self._runtime_unavailable_reason: Optional[str] = None
        self._runtime_unavailable_at: float = 0.0
        # Managed profiles use the profile-owned reasoning contract.  A known
        # profile that explicitly disables reasoning has no generic
        # fast/thinking mode; always-thinking profiles have one canonical
        # ``always`` mode; unknown external endpoints retain legacy fast.
        self._profile_reasoning_supported = profile_reasoning
        self._current_llm_mode = (
            resolve_strata_response_mode(config)
            if self._managed_local_runtime == "strata"
            else "always"
            if self._profile_thinking_mode == "always"
            else self._effective_reasoning_effort()
            or ("" if profile_reasoning is False else "fast")
        )
        self.system_prompt = (
            "You are a concise assistant. Follow the supplied instruction "
            "exactly and do not use tools."
            if self._lightweight_ephemeral_client
            else self._build_system_prompt()
        )

        logger.info("[OpenAICompatibleLocalClient] initialized")
        logger.info("[OpenAICompatibleLocalClient] Base URL: %s", self.base_url)
        logger.info("[OpenAICompatibleLocalClient] Model: %s", self.model_name)
        logger.info(
            "[OpenAICompatibleLocalClient] tools=auto response_format=%s extra_body=%s",
            self.enable_response_format,
            self.enable_extra_body,
        )

    def _get_session_user_id(self) -> str:
        if self.session_user_id:
            return self.session_user_id
        metadata_user_id = self.session_metadata.get("user_id")
        return str(metadata_user_id) if metadata_user_id else "default_user"

    def _sync_privacy_gateway(self) -> OutboundPrivacyGateway:
        turn = get_turn_context()
        user_id = str(self._get_session_user_id() or "default_user")
        session_id = str(getattr(self, "current_session_id", None) or "")
        if bool(getattr(turn, "suppress_automatic_context", False)):
            # Help is a one-turn controller scope.  Never reuse a normal
            # request's reversible aliases or policy metadata, including for
            # direct provider calls that bypass TerminalMode's snapshot.
            gateway = getattr(self, "_privacy_gateway", None)
            isolated_user_id = str(getattr(turn, "user_id", None) or user_id)
            isolated_session_id = str(
                getattr(turn, "session_id", None) or session_id
            )
            if (
                not bool(getattr(gateway, "_aoitalk_help_isolated", False))
                or getattr(gateway, "user_id", None) != isolated_user_id
                or getattr(gateway, "session_id", None) != isolated_session_id
            ):
                gateway = OutboundPrivacyGateway(
                    getattr(self, "config", None),
                    user_id=isolated_user_id,
                    session_id=isolated_session_id,
                    session_context={},
                    project_metadata={},
                )
                setattr(gateway, "_aoitalk_help_isolated", True)
                self._privacy_gateway = gateway
            else:
                gateway.update_policy_context(
                    session_context={},
                    project_metadata={},
                )
            return gateway
        if (
            self._privacy_gateway.user_id != user_id
            or self._privacy_gateway.session_id != session_id
        ):
            self._privacy_gateway = OutboundPrivacyGateway(
                self.config,
                user_id=user_id,
                session_id=session_id,
                session_context=self._privacy_session_context,
                project_metadata=self._privacy_project_metadata,
            )
        else:
            if self._privacy_session_context or self._privacy_project_metadata:
                self._privacy_gateway.update_policy_context(
                    session_context=self._privacy_session_context,
                    project_metadata=self._privacy_project_metadata,
                )
            else:
                self._privacy_gateway.update_policy_context()
        return self._privacy_gateway

    def _get_memory_metadata(self) -> Dict[str, Any]:
        metadata = self.session_metadata.copy() if self.session_metadata else {}
        manifest = context_manifest_metadata(self)
        if manifest is not None:
            metadata["context_manifest"] = manifest
        return metadata

    def get_generation_metadata(self) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {}
        if self._last_generation_metrics:
            metadata["generation_metrics"] = dict(self._last_generation_metrics)
        if self._last_context_snapshots:
            latest = context_snapshot_without_manifest_capture(
                self._last_context_snapshots[-1]
            )
            latest["requests"] = [
                context_snapshot_without_manifest_capture(item)
                for item in self._last_context_snapshots[-8:]
            ]
            metadata["context_snapshot"] = latest
        if self._last_model_transcript:
            metadata["model_transcript"] = [dict(item) for item in self._last_model_transcript]
        metadata["cache_usage"] = dict(self._last_usage)
        metadata["cache_diagnostics"] = {
            "provider": "openai_compatible_local",
            "model": self.model_name,
            "server_profile": self.server_profile.get("name"),
            "cache_mode": self.server_profile.get("cache_mode"),
            "cache_supported": self.server_profile.get("cache_supported"),
            "cache_active": self._last_usage.get("cache_active"),
            "cache_configured": self.server_profile.get("cache_supported") is True,
            "metrics_source": self.server_profile.get("metrics_source"),
            "cache_key": getattr(self, "_cache_key", None),
        }
        manifest = context_manifest_metadata(
            self,
            allow_snapshot_only=True,
        )
        if manifest is not None:
            metadata["context_manifest"] = manifest
        from ..services.memory_selection_observation import observation_for_current_turn
        memory_observation = observation_for_current_turn(
            getattr(self, "_last_memory_selection_observation", None)
        )
        if memory_observation is not None:
            metadata["memory_selection_observation"] = memory_observation
        return metadata

    def _capture_context_request(self, api_kwargs: Dict[str, Any], *, reason: str) -> None:
        suppress_automatic_context = bool(
            getattr(get_turn_context(), "suppress_automatic_context", False)
        )
        budget = self._current_context_budget
        # Use the same Project Context OFF projection as prompt construction;
        # context snapshots must not retain stale project/task/work layers
        # merely because a prior turn populated ``_current_context_bundle``.
        stable_bundle, dynamic_bundle, bundle_parts = (
            context_bundle_partition_components(self._context_bundle_for_turn())
        )
        observed_messages = list(api_kwargs.get("messages", []))
        for label, value in (
            ("Stable Project Context", stable_bundle),
            ("Current context bundle", dynamic_bundle),
        ):
            if value:
                observed_messages = without_text(
                    observed_messages, f"[{label}]\n{value.strip()}"
                )
        parts = [
            *message_components(observed_messages),
            *bundle_parts,
            *tool_components(api_kwargs.get("tools", []), source="chat.completions tools payload"),
        ]
        active_names = {
            str(((item.get("function") or {}).get("name") or ""))
            for item in api_kwargs.get("tools", []) if isinstance(item, dict)
        }
        if not suppress_automatic_context:
            for tool_def in self._tool_registry.get_all():
                name = str(getattr(tool_def, "name", "") or "")
                if name and name not in active_names:
                    parts.append(component(
                        "native_tool_schemas", "Native tool schemas", source="runtime tool registry",
                        status="deferred", tokens=0, preview=f"{name}（未送信）",
                    ))
        item = snapshot(
            provider="openai_compatible_local",
            model=self.model_name,
            components=parts,
            context_window_tokens=budget.context_window_tokens if budget else None,
            response_tokens=int(api_kwargs.get("max_tokens") or (budget.response_tokens if budget else 0)),
            request_index=len(self._last_context_snapshots),
            request_kind=reason,
            window_source=budget.source if budget else None,
        )
        self._last_context_snapshots.append(item)
        self._last_context_snapshots = self._last_context_snapshots[-8:]

    def _capture_generation_metrics(self, response: Any) -> Dict[str, Any]:
        if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            # A direct Help call may reuse a provider instance whose previous
            # usage is still attached.  Do not replace that ordinary
            # diagnostic with controller-turn telemetry (the outer terminal
            # snapshot restores it for normal orchestration).
            self._last_usage = {}
            self._last_generation_metrics = None
            return {}
        payload = _as_plain_dict(response)
        timings = _as_plain_dict(payload.get("timings"))
        usage = _as_plain_dict(payload.get("usage"))
        normalized_usage = normalize_usage(
            usage,
            provider="openai_compatible_local",
            resolved_model=(
                str(payload.get("model")) if payload.get("model") else None
            ),
        )
        self._last_usage = {
            key: value for key, value in normalized_usage.items() if value is not None
        }

        tokens_per_second = _as_finite_float(
            _first_present(
                timings.get("predicted_per_second"),
                timings.get("completion_per_second"),
                timings.get("eval_per_second"),
            )
        )
        output_tokens = _as_non_negative_int(
            _first_present(
                timings.get("predicted_n"),
                usage.get("completion_tokens"),
                usage.get("output_tokens"),
            )
        )
        prompt_tokens = _as_non_negative_int(
            _first_present(usage.get("prompt_tokens"), usage.get("input_tokens"))
        )
        total_tokens = _as_non_negative_int(usage.get("total_tokens"))
        predicted_ms = _as_finite_float(timings.get("predicted_ms"))
        prompt_ms = _as_finite_float(timings.get("prompt_ms"))

        if tokens_per_second is None and output_tokens and predicted_ms and predicted_ms > 0:
            tokens_per_second = output_tokens / (predicted_ms / 1000)

        if tokens_per_second is None and output_tokens is None:
            self._last_generation_metrics = None
            return dict(self._last_usage)

        metrics: Dict[str, Any] = {
            "provider": "openai_compatible_local",
            "model": self.model_name,
        }
        if tokens_per_second is not None and tokens_per_second >= 0:
            metrics["tokens_per_second"] = round(tokens_per_second, 2)
        if output_tokens is not None:
            metrics["output_tokens"] = output_tokens
        if prompt_tokens is not None:
            metrics["prompt_tokens"] = prompt_tokens
        if total_tokens is not None:
            metrics["total_tokens"] = total_tokens
        if predicted_ms is not None:
            metrics["generation_ms"] = round(predicted_ms, 3)
        if prompt_ms is not None:
            metrics["prompt_ms"] = round(prompt_ms, 3)
        self._last_generation_metrics = metrics
        if prompt_tokens is not None and self._last_context_snapshots:
            self._last_context_snapshots[-1] = reconcile_snapshot(
                self._last_context_snapshots[-1], prompt_tokens
            )
        return dict(self._last_usage)

    def _ensure_request_usage_ledger(self) -> None:
        records = getattr(self, "_request_usage_records", None)
        if not isinstance(records, list):
            self._request_usage_records = []
        if not isinstance(getattr(self, "_request_usage_unknown", None), bool):
            self._request_usage_unknown = False
        if not isinstance(getattr(self, "_request_usage_sequence", None), int):
            self._request_usage_sequence = 0

    def _reset_request_usage_ledger(self) -> None:
        self._request_usage_records = []
        self._request_usage_unknown = False
        self._request_usage_sequence = 0

    def _mark_request_usage_unknown(self) -> None:
        if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            return
        self._ensure_request_usage_ledger()
        self._request_usage_unknown = True

    def _record_request_usage(self, usage: Mapping[str, Any] | None) -> None:
        """Record one actual local provider invocation without estimation."""

        if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            return
        self._ensure_request_usage_ledger()
        self._request_usage_sequence += 1
        if not isinstance(usage, Mapping) or not usage or not any(
            usage.get(key) is not None
            for key in (
                "input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "cached_tokens",
                "total_tokens",
                "cost_micros",
            )
        ):
            self._request_usage_unknown = True
            return
        record = dict(usage)
        record["usage_key"] = f"local:{self._request_usage_sequence}"
        self._request_usage_records.append(record)

    def _project_request_usage_to_agent_run(self) -> None:
        """Expose the current local request ledger to terminal Agent events."""

        self._ensure_request_usage_ledger()
        if self._request_usage_unknown or not self._request_usage_records:
            # An absent projection is intentional: AgentRun accounting then
            # records unknown rather than charging an inferred zero.
            self._agent_dialogue_usage = {}
            self._last_usage_records = []
            return
        totals: dict[str, Any] = {}
        for record in self._request_usage_records:
            for key in (
                "input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "cached_tokens",
                "total_tokens",
                "cost_micros",
            ):
                value = record.get(key)
                if value is None:
                    continue
                try:
                    totals[key] = totals.get(key, 0) + max(0, int(value))
                except (TypeError, ValueError, OverflowError):
                    self._request_usage_unknown = True
                    self._agent_dialogue_usage = {}
                    self._last_usage_records = []
                    return
        if not totals:
            self._agent_dialogue_usage = {}
            self._last_usage_records = []
            return
        reasoning_flags = {
            record.get("output_includes_reasoning")
            for record in self._request_usage_records
            if isinstance(record.get("output_includes_reasoning"), bool)
        }
        if len(reasoning_flags) == 1:
            totals["output_includes_reasoning"] = next(iter(reasoning_flags))
        total_sources = {
            record.get("total_tokens_source")
            for record in self._request_usage_records
            if record.get("total_tokens_source") in {"reported", "derived"}
        }
        if len(total_sources) == 1:
            totals["total_tokens_source"] = next(iter(total_sources))
        totals["usage_state"] = "known"
        self._agent_dialogue_usage = totals
        self._last_usage_records = [
            dict(record) for record in self._request_usage_records
        ]

    def _capture_and_persist_usage(
        self,
        response: Any,
        *,
        request_type: str = "chat",
        latency_ms: int = 0,
        is_streaming: bool = False,
    ) -> Dict[str, Any]:
        """Capture and persist one successful local completion response."""

        if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            self._capture_generation_metrics(response)
            return {}

        usage = self._capture_generation_metrics(response)
        self._record_request_usage(usage)
        if usage:
            persist_usage_sync(
                self,
                provider="openai_compatible_local",
                model=self.model_name,
                usage=usage,
                request_type=request_type,
                latency_ms=latency_ms,
                is_streaming=is_streaming,
            )
        return usage

    def _is_help_isolated_turn(self) -> bool:
        """Return True only for the Guide-only Help isolation boundary.

        suppress_automatic_context is shared by Help and trusted background
        controllers such as Project Steward. Background controllers install
        their own isolated system prompt and a strict server-owned tool scope;
        treating every suppressed turn as Help drops that prompt and also
        disables local-model recovery paths.
        """

        turn = get_turn_context()
        if not bool(getattr(turn, "suppress_automatic_context", False)):
            return False
        strict_background_scope = (
            bool(getattr(turn, "strict_project_scope", False))
            or get_strict_tool_allowlist() is not None
        )
        command_capabilities = {
            str(value).strip().casefold()
            for value in (getattr(self, "current_command_capabilities", ()) or ())
            if str(value).strip()
        }
        return "aoitalk_help" in command_capabilities or not strict_background_scope

    def _build_system_prompt(self) -> str:
        if self._is_help_isolated_turn():
            return AOITALK_HELP_ISOLATED_SYSTEM_PROMPT
        if not self.config:
            return "あなたは親切なAIアシスタントです。"
        if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            custom_instructions = ""
        else:
            try:
                custom_instructions = get_user_custom_instructions_sync(
                    self._get_session_user_id()
                )
            except Exception as exc:
                logger.debug(
                    "[OpenAICompatibleLocalClient] Failed to load custom instructions: %s",
                    exc,
                )
                custom_instructions = ""
        try:
            return build_unified_instructions(
                character_name=self.character_name,
                config=self.config,
                include_static_tool_reference=False,
                custom_instructions=custom_instructions,
                # This client uses OpenAI-compatible function tool calls (or
                # a tool-free completion), never the CLI textual protocol.
                tool_protocol="native",
            )
        except Exception as exc:
            logger.warning(
                "[OpenAICompatibleLocalClient] Falling back to basic prompt: %s", exc
            )
            return f"あなたは{self.character_name}です。"

    def _build_effective_system_prompt(self) -> str:
        if self._is_help_isolated_turn():
            # A stale system/isolated override from an ordinary turn is not
            # permitted to widen the Guide-only Help prompt.
            return AOITALK_HELP_ISOLATED_SYSTEM_PROMPT
        override = str(
            getattr(self, "_isolated_system_prompt_override", "") or ""
        ).strip()
        if override:
            return override
        # Keep compatibility with lightweight clients that still expose the
        # legacy private override attribute.
        override = str(getattr(self, "_system_prompt_override", "") or "").strip()
        if override:
            return override
        return getattr(self, "system_prompt", "")

    def set_character(self, character_name: str) -> None:
        self.character_name = character_name
        self._isolated_system_prompt_override = ""
        self.system_prompt = self._build_system_prompt()

    def update_character(self, yaml_filename: str) -> None:
        if not self.config:
            return
        character_config = self.config.get_character_config(yaml_filename)
        self.character_name = character_config.get("name", yaml_filename)
        self._isolated_system_prompt_override = ""
        self.clear_history()
        self.system_prompt = self._build_system_prompt()

    def set_system_prompt(self, prompt: str) -> None:
        self._isolated_system_prompt_override = ""
        if self.config:
            self.system_prompt = self._build_system_prompt()
            return
        self.system_prompt = prompt

    def set_isolated_system_prompt(self, prompt: str) -> None:
        isolated_prompt = str(prompt or "").strip()
        self._isolated_system_prompt_override = isolated_prompt
        self.system_prompt = isolated_prompt

    def _reasoning_effort_metadata(self) -> Optional[Dict[str, Any]]:
        if _llama_cpp_profile_capability(
            getattr(self, "_llama_cpp_profile", None),
            "reasoning",
        ) is False:
            return None
        return llama_cpp_reasoning_effort_metadata(self.model_name)

    def _configured_reasoning_effort(self) -> Optional[str]:
        metadata = self._reasoning_effort_metadata()
        if not metadata:
            return None
        values = (
            _config_get(self.config, "runtime.target_reasoning_effort"),
            _config_get(
                self.config,
                "openai_compatible_local.llama_cpp.reasoning_effort",
            ),
            _config_get(self.config, "openai_compatible_local.reasoning_effort"),
        )
        for value in values:
            normalized = str(value or "").strip().lower()
            if normalized in metadata["options"]:
                return normalized
        return None

    def _effective_reasoning_effort(self) -> Optional[str]:
        metadata = self._reasoning_effort_metadata()
        if not metadata:
            return None
        configured = self._configured_reasoning_effort()
        return configured or str(metadata["default"])

    def set_llm_mode(self, mode: str) -> None:
        if getattr(self, "_profile_thinking_mode", None) == "always":
            normalized = str(mode or "").strip().lower()
            # Older persisted routes may still carry ``fast`` or
            # ``thinking``.  Accept those aliases during a model switch but
            # canonicalize all of them to ``always``; none can disable the
            # model's official reasoning template.
            if normalized in {"", "fast", "thinking"}:
                normalized = "always"
            if normalized != "always":
                raise ValueError(
                    "Managed local profile only supports always-thinking mode"
                )
            self._current_llm_mode = "always"
            return
        metadata = self._reasoning_effort_metadata()
        if metadata:
            normalized = str(mode or "").strip().lower()
            if normalized not in metadata["options"]:
                raise ValueError(
                    "Unsupported reasoning effort for managed local profile: "
                    f"{mode!r}; expected one of {metadata['options']}"
                )
            self._current_llm_mode = normalized
            return
        if getattr(self, "_profile_reasoning_supported", None) is False:
            # Do not persist or expose a synthetic fast/thinking value for a
            # known profile whose registry explicitly disables reasoning.
            self._current_llm_mode = ""
            return
        if mode not in {"fast", "thinking"}:
            mode = "fast"
        self._current_llm_mode = mode

    def get_llm_mode(self) -> str:
        return self._current_llm_mode

    @property
    def current_session_id(self) -> Optional[str]:
        return self._current_session_id

    @current_session_id.setter
    def current_session_id(self, value: Optional[str]) -> None:
        normalized = str(value).strip() if value else None
        if normalized and normalized != self._history_session_id:
            if hasattr(self, "history_manager"):
                self.history_manager.clear()
            self._history_session_id = normalized
            self._context_window_override_tokens = None
            logger.debug(
                "[OpenAICompatibleLocalClient] Cleared local history for session switch: %s",
                normalized,
            )
        self._current_session_id = normalized

    def set_session_context(
        self,
        user_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._isolated_system_prompt_override = ""
        if user_id:
            self.session_user_id = str(user_id)
        if metadata:
            sanitized = {k: str(v) for k, v in metadata.items() if v is not None}
            self.session_metadata = {**self.session_metadata, **sanitized}
            if "privacy_mode" in metadata:
                self._privacy_session_context["privacy_mode"] = str(
                    metadata.get("privacy_mode") or ""
                )
        # Lightweight controllers supply their own messages. Owner binding
        # must preserve the constructor's prompt/context isolation contract.
        if not getattr(self, "_lightweight_ephemeral_client", False):
            self.system_prompt = self._build_system_prompt()

    def _include_project_context_enabled(self) -> bool:
        return project_context_enabled_for_client(self)

    def _context_bundle_for_turn(
        self,
        bundle: Optional[ContextBundle] = None,
    ) -> Optional[ContextBundle]:
        """Hide stale selected-Project layers when Project Context is OFF.

        The normal ContextBuilder call already receives the immutable turn
        flag, but direct-client fallbacks and test doubles may hand this
        provider a previously-built bundle.  Never let that stale bundle
        re-introduce the selected project's scope into a turn that explicitly
        disabled Project Context; user/session memory remains available.
        """

        current = (
            getattr(self, "_current_context_bundle", None)
            if bundle is None
            else bundle
        )
        if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            return None
        if current is None or self._include_project_context_enabled():
            return current
        return strip_project_context_bundle(current)

    def _resolve_strata_mode(self) -> Optional[str]:
        """Resolve the live Strata mode without consulting stale private state.

        ``get_llm_mode`` is the canonical adapter boundary for the selected
        mode.  A missing, empty, unknown, or failing getter is a controlled
        configuration failure; falling back to ``_current_llm_mode`` or saved
        config could emit a wire mode the caller did not select.
        """

        if getattr(self, "_managed_local_runtime", None) != "strata":
            return None
        getter = getattr(self, "get_llm_mode", None)
        if not callable(getter):
            raise StrataModeResolutionError(
                "Strata client does not expose a live response mode getter"
            )
        try:
            raw_mode = getter()
        except Exception as exc:
            raise StrataModeResolutionError(
                "Strata live response mode getter failed"
            ) from exc
        mode = str(raw_mode or "").strip().lower()
        if mode not in FAST_THINKING_MODE_OPTIONS:
            raise StrataModeResolutionError(
                "Strata live response mode must be fast or thinking"
            )
        return mode

    def _strata_thinking_mode_selected(self) -> bool:
        """Return whether the canonical live Strata mode is Thinking."""

        return self._resolve_strata_mode() == "thinking"

    def _scoped_response_tokens_override(self) -> Optional[int]:
        """Return the private, curator-only response reserve when authorized."""

        if not self._strata_thinking_mode_selected():
            return None
        raw = getattr(self, "_curator_synthesis_output_limit", None)
        value = _as_non_negative_int(raw)
        if value is None or value < 256:
            return None
        return min(value, STRATA_THINKING_RESPONSE_TOKENS_LIMIT)

    def _configured_output_limit(self) -> Optional[int]:
        """Return the request-local positive output cap, when configured."""

        config = getattr(self, "config", None)
        getter = getattr(config, "get", None)
        if not callable(getter):
            return (
                ALWAYS_THINKING_DEFAULT_OUTPUT_TOKENS
                if getattr(self, "_profile_thinking_mode", None) == "always"
                else None
            )
        try:
            raw = getter("runtime.target_max_output_tokens", None)
            value = int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError, OverflowError):
            value = None
        if value is not None and value > 0:
            return value
        return (
            ALWAYS_THINKING_DEFAULT_OUTPUT_TOKENS
            if getattr(self, "_profile_thinking_mode", None) == "always"
            else None
        )

    def _context_budget(
        self,
        max_tokens: Optional[int] = None,
        *,
        response_tokens_override: Optional[int] = None,
    ) -> ContextBudget:
        scoped_override = (
            response_tokens_override
            if response_tokens_override is not None
            else self._scoped_response_tokens_override()
        )
        if not self._strata_thinking_mode_selected():
            scoped_override = None
        return resolve_context_budget(
            config=self.config,
            provider_key="openai_compatible_local",
            base_url=self.base_url,
            model_name=self.model_name,
            api_key=self.api_key,
            requested_max_tokens=max_tokens,
            override_context_window_tokens=self._context_window_override_tokens,
            response_tokens_override=scoped_override,
        )

    def _build_messages(
        self,
        user_input: str,
        context_budget: ContextBudget,
    ) -> List[Dict[str, str]]:
        suppress_automatic_context = bool(
            getattr(get_turn_context(), "suppress_automatic_context", False)
        )
        system_prompt = self._system_prompt_for_budget(context_budget)
        context_window = self.history_manager.context_window_size
        history_limit = min(context_window * 2, context_budget.history_messages)
        history = (
            []
            if suppress_automatic_context
            else self.history_manager.get_model_messages()[-history_limit:]
        )
        messages = [
            {"role": "system", "content": system_prompt},
            *build_prompt_messages(
                history,
                summary=("" if suppress_automatic_context else self.history_manager.summary),
                current_user_input=clip_text_preserve_tail(
                    user_input, context_budget.message_budget_chars
                ),
            ),
        ]
        messages = [
            {
                **message,
                "content": (
                    clip_keeping_common_block(
                        str(message.get("content") or ""),
                        system_clip_limit(
                            str(message.get("content") or ""),
                            context_budget.history_message_chars,
                        )
                        if message.get("role") == "system"
                        else context_budget.history_message_chars,
                        clip_text,
                    )
                    if message.get("role") != "user" or message is not messages[-1]
                    else message.get("content")
                ),
            }
            for message in messages
        ]
        # The owned system instructions (role/task body plus the common
        # block) are charged to the message budget in full; the user turn
        # must still fit beside them or the request is refused before send.
        system_chars = len(str(messages[0].get("content") or "")) + 24
        if (
            str(messages[0].get("content") or "").endswith(COMMON_HARNESS_INSTRUCTIONS)
            and context_budget.message_budget_chars - system_chars - 24
            < MIN_USER_TURN_CHARS
        ):
            raise SystemInstructionsExceedContextBudget(
                "system instructions (role/task + common execution rules) use "
                f"{system_chars} of the {context_budget.message_budget_chars}-char "
                "message budget, leaving no room for the user turn"
            )
        return self._fit_messages_to_context_budget(messages, context_budget)

    def _fit_messages_to_context_budget(
        self,
        messages: List[Dict[str, str]],
        context_budget: ContextBudget,
    ) -> List[Dict[str, str]]:
        def payload_chars(items: List[Dict[str, str]]) -> int:
            return sum(len(str(item.get("content") or "")) + 24 for item in items)

        compacted = list(messages)
        while (
            len(compacted) > 2
            and payload_chars(compacted) > context_budget.message_budget_chars
        ):
            compacted.pop(1)
        if payload_chars(compacted) > context_budget.message_budget_chars and compacted:
            # Both message envelopes are charged; the user turn gets only what
            # remains beside the (never clipped) system message.
            allowance = (
                context_budget.message_budget_chars
                - len(str(compacted[0].get("content") or ""))
                - 48
            )
            if len(compacted) < 2 or allowance < 1:
                raise SystemInstructionsExceedContextBudget(
                    "system instructions leave no room for the user turn within "
                    f"the {context_budget.message_budget_chars}-char message budget"
                )
            content = clip_text_preserve_tail(
                str(compacted[-1].get("content") or ""), allowance
            )
            compacted[-1] = {**compacted[-1], "content": content[:allowance]}
        return compacted

    def _is_constrained_context_budget(self, context_budget: ContextBudget) -> bool:
        return context_budget.context_window_tokens <= 16384

    def _system_prompt_for_budget(self, context_budget: ContextBudget) -> str:
        if self._is_help_isolated_turn():
            return AOITALK_HELP_ISOLATED_SYSTEM_PROMPT
        override = str(
            getattr(self, "_isolated_system_prompt_override", "") or ""
        ).strip()
        if override:
            return override
        override = str(getattr(self, "_system_prompt_override", "") or "").strip()
        if override:
            return override
        if not self._is_constrained_context_budget(context_budget):
            return self.system_prompt
        return with_common_harness_instructions("\n".join(
            [
                "You are AoiTalk's assistant. Answer in the user's language.",
                "Docs are AoiTalk's KnowledgeNode outline, Projects are case scopes, and Workspaces store files; keep these concepts separate.",
                "When Project Context is OFF, the Selected Project is only weak UI state, not the request target; inspect Project information only when the request requires it.",
                "Use tool hints as short candidate reminders and decide tool use from the full request.",
                "Do not claim search, file reading, project updates, time/weather lookup, or calculation unless a successful tool result exists.",
                "Do not invent case details. If evidence is insufficient, say what is missing and answer only from available facts.",
                "For file or workspace questions, trust successful filesystem inspection results included in the user message.",
                "Do not claim a file could not be read when the provided evidence says it was read.",
            ]
        ))

    def _context_parts_for_budget(
        self,
        context_bundle: Optional[ContextBundle],
        context_budget: ContextBudget,
        *,
        has_tool_hints: bool,
    ) -> tuple[str, str]:
        """Return cache-stable Project and turn-local context under one budget."""

        if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            return "", ""
        if not context_bundle:
            return "", ""
        bundle = context_bundle
        if self.history_manager.summary and getattr(bundle, "session_context_block", ""):
            bundle = dataclasses.replace(bundle, session_context_block="")
        if not self._is_constrained_context_budget(context_budget):
            return bundle.render_partitioned_for_prompt(
                context_budget.context_bundle_chars
            )
        if has_tool_hints:
            # Preserve the previous compact-tool behavior: retain Project
            # identity and relevant detail, but keep Memory/session/work state
            # out of an already constrained prompt.  Stable + dynamic still
            # share the same 1,400-character ceiling.
            limit = min(1400, context_budget.context_bundle_chars)
            stable = clip_text(
                str(bundle.project_context_block or "").strip(),
                min(700, limit),
            )
            remaining = max(0, limit - len(stable) - (2 if stable else 0))
            dynamic = "\n\n".join(
                block.strip()
                for block in (
                    getattr(bundle, "project_details_block", ""),
                    bundle.project_information_block,
                )
                if block and block.strip()
            )
            return stable, clip_text(dynamic, remaining) if remaining else ""
        return bundle.render_partitioned_for_prompt(
            context_budget.context_bundle_chars
        )

    def _context_block_for_budget(
        self,
        context_bundle: Optional[ContextBundle],
        context_budget: ContextBudget,
        *,
        has_tool_hints: bool,
    ) -> str:
        """Compatibility projection for callers that still expect one block."""

        stable, dynamic = self._context_parts_for_budget(
            context_bundle,
            context_budget,
            has_tool_hints=has_tool_hints,
        )
        return "\n\n".join(part for part in (stable, dynamic) if part)

    def _build_model_messages_for_budget(
        self,
        user_input: str,
        project_context: Optional[dict[str, Any]],
        context_budget: ContextBudget,
    ) -> tuple[List[Dict[str, str]], str]:
        suppress_automatic_context = bool(
            getattr(get_turn_context(), "suppress_automatic_context", False)
        )
        self._current_context_bundle = self._build_context_bundle_sync(
            user_input,
            project_context,
            context_budget,
        )
        # Keep direct/fallback ContextBuilder implementations subject to the
        # immutable Project Context OFF boundary as well.
        context_bundle = (
            None if suppress_automatic_context else self._context_bundle_for_turn()
        )
        self._current_tool_hint_catalog_names = ()
        self._current_tool_hint_carrier_prefix_sha256 = None
        tool_hint_context = (
            ""
            if suppress_automatic_context
            else self._build_tool_hint_context(user_input, context_budget)
        )
        self._current_tool_hint_context = tool_hint_context
        model_user_input = compose_tool_hint_user_message(
            user_input,
            "",
        )
        stable_block, dynamic_block = self._context_parts_for_budget(
            context_bundle,
            context_budget,
            has_tool_hints=bool(tool_hint_context),
        )
        stable_context: list[tuple[str, str]] = []
        dynamic_context: list[tuple[str, str]] = []
        if stable_block:
            stable_context.append(("Stable Project Context", stable_block))
        if dynamic_block:
            dynamic_context.append(("Current context bundle", dynamic_block))
        if tool_hint_context:
            dynamic_context.append(("Current tool hints", tool_hint_context))
        self._current_prompt_stable_context = list(stable_context)
        self._current_prompt_dynamic_context = list(dynamic_context)
        self._current_project_cache_revision = str(
            getattr(context_bundle, "project_cache_revision", "") or ""
        )
        # Keep the stable system prefix independent from the current date and
        # current-turn retrieval/tool hints.
        isolated_override = str(
            getattr(self, "_isolated_system_prompt_override", "") or ""
        ).strip()
        system_content = self._system_prompt_for_budget(context_budget)
        if not isolated_override:
            system_content = f"{system_content}\n\n{_current_date_context()}"
        prompt_kwargs = dict(
            summary=("" if suppress_automatic_context else self.history_manager.summary),
            current_user_input=clip_text_preserve_tail(
                model_user_input, context_budget.message_budget_chars
            ),
            stable_context=([] if suppress_automatic_context else stable_context),
            dynamic_context=([] if suppress_automatic_context else dynamic_context),
        )
        history: list[dict[str, Any]] = []
        if not suppress_automatic_context:
            # History is selected with a start that only moves when a limit is
            # crossed (see stable_history_window), so consecutive turns send
            # the same prefix and the engine's KV/prefix cache is reusable.
            fixed_chars = len(system_content) + 24 + sum(
                len(str(item.get("content") or "")) + 24
                for item in build_prompt_messages([], **prompt_kwargs)
            )
            history = stable_history_window(
                self.history_manager.get_model_messages(),
                max_messages=context_budget.history_messages,
                max_chars=context_budget.message_budget_chars - fixed_chars,
            )
        model_messages = build_prompt_messages(history, **prompt_kwargs)
        messages = [
            {
                "role": "system",
                "content": system_content,
            },
            *model_messages,
        ]
        fitted_messages = self._fit_messages_to_context_budget(messages, context_budget)
        self._current_tool_hint_carrier_prefix_sha256 = (
            _catalog_hint_carrier_prefix_digest(fitted_messages, tool_hint_context)
        )
        return (
            fitted_messages,
            tool_hint_context,
        )

    def _retry_after_context_overflow(
        self,
        *,
        user_input: str,
        project_context: Optional[dict[str, Any]],
        temperature: float,
        max_tokens: Optional[int],
        required_tool_name: Optional[str] = None,
    ) -> str:
        for _ in range(2):
            context_budget = self._context_budget(max_tokens)
            self._current_context_budget = context_budget
            try:
                messages, tool_hint_context = self._build_model_messages_for_budget(
                    user_input,
                    project_context,
                    context_budget,
                )
                return self.chat(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    request_type="retry",
                    tools_enabled=bool(required_tool_name),
                    context_budget=context_budget,
                    fallback_user_input=user_input,
                    required_tool_name=required_tool_name,
                    native_tool_user_input=user_input,
                )
            except Exception as retry_exc:
                if not _is_context_overflow_error(retry_exc):
                    raise
                self._reduce_context_budget_after_overflow()
                logger.warning(
                    "[OpenAICompatibleLocalClient] compact retry still exceeded context: %s",
                    retry_exc,
                )
        try:
            context_budget = self._context_budget(max_tokens)
            self._current_context_budget = context_budget
            return self._plain_empty_response_retry(
                user_input,
                temperature=temperature,
                max_tokens=max_tokens,
                context_budget=context_budget,
                reason="context overflow",
            )
        except Exception as final_retry_exc:
            logger.warning(
                "[OpenAICompatibleLocalClient] minimal overflow retry failed: %s",
                final_retry_exc,
            )
        return ""

    def _run_async_sync(self, coro):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()

    def _resolve_project_context_sync(self) -> Optional[dict[str, Any]]:
        if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            # AoiTalk Help is Guide-only; do not perform a selected
            # session/Project resolver lookup before the stateless generation.
            return None
        current_project_id = getattr(self, "current_project_id", None)
        current_session_id = getattr(self, "current_session_id", None)
        if not current_project_id and not current_session_id:
            return None

        resolver = ProjectContextResolver()
        try:
            return self._run_async_sync(
                resolver.resolve_context(
                    project_id=current_project_id,
                    session_id=current_session_id,
                    user_id=self._get_session_user_id(),
                )
            )
        except Exception as exc:
            logger.warning(
                "[OpenAICompatibleLocalClient] Failed to resolve project context: %s",
                exc,
            )
            return None

    def _build_tool_hint_context(
        self,
        user_input: str,
        context_budget: ContextBudget,
    ) -> str:
        if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            return ""
        filtered_registry = filtered_registry_for_client(self, self._tool_registry)
        tool_hint_context = build_tool_hint_context_sync(
            user_input=user_input,
            registry=filtered_registry,
            policy=get_client_generation_policy(self),
            log_prefix="OpenAICompatibleLocalClient",
            max_result_chars=context_budget.tool_hint_context_chars,
        )
        available_names = {
            str(name or "").strip()
            for name in filtered_registry.get_names()
            if str(name or "").strip()
        }
        self._current_tool_hint_catalog_names = tuple(
            _canonical_catalog_names(
                [
                    name
                    for name in re.findall(
                        r"`([A-Za-z_][A-Za-z0-9_]*)`",
                        str(tool_hint_context or ""),
                    )
                    if name in available_names
                ]
            )
            or ()
        )
        return tool_hint_context

    def _required_command_tool_name(self, user_input: str) -> Optional[str]:
        if "web_search" in set(
            getattr(self, "current_command_capabilities", ()) or ()
        ):
            return "web_search"
        if command_capability_active(user_input, "web_search"):
            return "web_search"
        return None

    def _required_project_management_tool_name(
        self,
        user_input: str,
    ) -> Optional[str]:
        policy = get_client_generation_policy(self)
        if policy.profile != GenerationProfile.AUTONOMOUS_WORK:
            return None
        if not (self.current_project_id or self.current_session_id):
            return None

        # The UI/controller capability is authority to perform a requested
        # mutation, not authority to override an explicit user prohibition.
        # Keep the turn read-only even when task_update/project_db_update was
        # carried on the client from the trusted command surface.
        if mutation_execution_forbidden(user_input):
            return None

        explicit_capabilities = set(
            getattr(self, "current_command_capabilities", ()) or ()
        ) | command_capabilities_from_text(user_input)
        if not explicit_capabilities.intersection(PROJECT_COMMAND_CAPABILITIES):
            # Natural-language task/DB/WBS wording is not proof that a
            # particular mutation is required.  Let the model choose the
            # appropriate direct tool instead of setting tool_choice to a
            # fixed function.
            return None

        required_tools: set[str] = set(
            project_management_required_mutation_tools(user_input)
        )
        # Terminal/voice callers also carry capabilities on the client while
        # passing the raw user text.  Preserve explicit command semantics even
        # when the trusted preamble is not present in ``user_input``.
        if "project_db_update" in explicit_capabilities:
            required_tools.update(
                {
                    "organize_project_information_from_folder",
                    "patch_project_information_doc",
                    "attach_project_information_reference",
                }
            )
        if "task_update" in explicit_capabilities:
            required_tools.update({"create_task", "update_task"})
        if "wbs_sync" in explicit_capabilities:
            required_tools.add("sync_wbs_tasks")
        # 強制するツールは実効集合（コア + ロード済み pack）の中から選ぶ。
        exposed = filtered_registry_for_client(self, self._tool_registry)
        for tool_name in (
            "organize_project_information_from_folder",
            "sync_wbs_tasks",
            "sync_issue_table",
            "patch_project_information_doc",
            "create_record_table",
            "create_task",
            "update_task",
        ):
            if tool_name in required_tools and tool_name in exposed:
                return tool_name
        return None

    def _requires_project_context_read(self, user_input: str | None) -> bool:
        return (
            project_context_enabled_for_client(self, default=False)
            and bool(self.current_project_id or self.current_session_id)
            and looks_like_project_management_request(str(user_input or ""))
        )

    def _is_managed_llama_cpp_runtime(self) -> bool:
        """True when AoiTalk may launch or relaunch an owned llama-server."""

        return self._can_attempt_managed_llama_cpp_recovery()

    def _can_attempt_managed_llama_cpp_recovery(self) -> bool:
        if not self.config:
            return False
        if managed_local_runtime_for_model(self.config, self.model_name) == "strata":
            from src.services.strata_runtime import strata_settings

            return bool(strata_settings(self.config, model=self.model_name)["auto_start"])
        try:
            from src.service_manager import llama_cpp_managed_launch_configured

            return llama_cpp_managed_launch_configured(
                self.config,
                model=self.model_name,
            )
        except Exception:
            return False

    def _managed_llama_cpp_launch_configuration_error(self) -> Optional[str]:
        if not self.config:
            return None
        if managed_local_runtime_for_model(self.config, self.model_name) == "strata":
            from src.services.strata_runtime import strata_launch_plan, strata_settings

            try:
                if strata_settings(self.config, model=self.model_name)["auto_start"]:
                    strata_launch_plan(self.config, model=self.model_name)
            except (OSError, ValueError) as exc:
                return str(exc)
            return None
        try:
            from src.service_manager import llama_cpp_managed_launch_configuration_error

            return llama_cpp_managed_launch_configuration_error(
                self.config,
                model=self.model_name,
            )
        except Exception:
            return None

    def _is_manual_managed_llama_cpp_runtime(self) -> bool:
        if not self.config:
            return False
        try:
            from src.service_manager import llama_cpp_manual_managed_runtime

            return llama_cpp_manual_managed_runtime(
                self.config,
                model=self.model_name,
            )
        except Exception:
            return False

    def _mark_runtime_unavailable(self, reason: str) -> None:
        detail = str(reason or "").strip()
        if not detail:
            return
        self._runtime_unavailable_reason = detail
        self._runtime_unavailable_at = time.monotonic()

    def _clear_runtime_unavailability(self) -> None:
        self._runtime_unavailable_reason = None
        self._runtime_unavailable_at = 0.0

    def is_runtime_known_unavailable(self) -> bool:
        return bool(str(self._runtime_unavailable_reason or "").strip())

    def _uses_llama_cpp_tool_choice_transport(self) -> bool:
        if managed_local_runtime_for_model(self.config, self.model_name) == "strata":
            return False
        if (
            getattr(self, "_managed_local_runtime", None) == "freetoken"
            or managed_local_runtime_for_model(self.config, self.model_name)
            == "freetoken"
        ):
            return False
        if self.server_profile.get("name") == "llama.cpp":
            return True
        if llama_cpp_model_profile(self.model_name) is not None:
            return True
        if not self.config:
            return False
        try:
            from src.service_manager._local_llm_servers import (
                _llama_cpp_selected_model,
                _llama_cpp_settings,
            )

            selected_model = _llama_cpp_selected_model(self.config, self.model_name)
            settings = _llama_cpp_settings(self.config, model=selected_model)
            return bool(str(settings.get("model_path") or "").strip())
        except Exception:
            return False

    def _is_llama_cpp_server(self) -> bool:
        return self._uses_llama_cpp_tool_choice_transport()

    def _required_llama_cpp_tool_request(self, api_kwargs: Dict[str, Any]) -> bool:
        choice = api_kwargs.get("tool_choice", "auto")
        extra_body = api_kwargs.get("extra_body")
        extra_body = extra_body if isinstance(extra_body, dict) else {}
        effective_choice = extra_body.get("tool_choice", choice)
        if choice in ("auto", "none") and effective_choice in ("auto", "none"):
            return False
        if not self._is_llama_cpp_server():
            return False
        for value in (choice, effective_choice):
            if not isinstance(value, str) or value not in {"auto", "none", "required"}:
                raise LocalToolSelectionError(
                    "llama.cppのtool_choiceはauto / none / requiredの文字列で指定してください。"
                    "非対応の指定をautoへ変更せず、送信を中止しました。"
                )
        if "required" not in (choice, effective_choice):
            return False
        if choice != effective_choice or (
            "tools" in extra_body and extra_body["tools"] != api_kwargs.get("tools")
        ):
            raise LocalToolSelectionError(
                "必須ツール指定とextra_bodyのtool_choice / toolsが競合しています。"
                "必須条件を変更せず、送信を中止しました。"
            )
        if not api_kwargs.get("tools"):
            raise LocalToolSelectionError(
                "必須ツールのschemaがリクエストにありません。送信を中止しました。"
            )
        return True

    @staticmethod
    def _tool_schema_snapshot(tools: Any) -> str:
        try:
            return json.dumps(tools, ensure_ascii=False, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise LocalToolSelectionError(
                "必須ツールのschemaをJSONとして検証できません。送信を中止しました。"
            ) from exc

    def _snapshot_required_tool_schema(self, payload: Dict[str, Any]) -> str | None:
        if not self._required_llama_cpp_tool_request(payload):
            return None
        # JSON preserves boolean/number types; only object key order is normalized.
        return self._tool_schema_snapshot(payload["tools"])

    def _validate_required_tool_contract(
        self, payload: Dict[str, Any], required_tool_schema: str | None
    ) -> None:
        if required_tool_schema is None:
            return
        if not self._required_llama_cpp_tool_request(payload):
            raise LocalToolSelectionError(
                "必須ツール指定が送信前のリクエストから失われました。送信を中止しました。"
            )
        extra_body = payload.get("extra_body")
        effective_tools = (
            extra_body.get("tools", payload["tools"])
            if isinstance(extra_body, dict)
            else payload["tools"]
        )
        if self._tool_schema_snapshot(effective_tools) != required_tool_schema:
            raise LocalToolSelectionError(
                "必須ツールのschemaが送信前に変更されました。"
                "元のschemaを保持できないため、送信を中止しました。"
            )

    def _format_last_tool_calls_evidence(self) -> str:
        return format_tool_execution_evidence(self._last_tool_calls)

    def _required_project_context_read_tool_name(
        self,
        user_input: str,
    ) -> Optional[str]:
        if not self._requires_project_context_read(user_input):
            return None

        # Grounding is request-local.  Once a successful project read has
        # been recorded during this turn, do not force the same read again on
        # an agentic continuation.  The audit ledger is reset at the start of
        # ``generate_response`` and deliberately excludes prior turns.
        if self._project_context_read_already_satisfied():
            return None

        # Task mutations need task identity/status/parent information as the
        # first grounding read.  Prefer ``list_tasks`` over a generic project
        # information read when it is available; the latter cannot resolve
        # ordinal task references such as "6, 7, 8".
        task_mutation_tools = {
            "create_task",
            "update_task",
            "delete_task",
            "assign_task",
            "schedule_task",
        }
        required_mutations = project_management_required_mutation_tools(user_input)
        exposed = filtered_registry_for_client(self, self._tool_registry)
        if required_mutations.intersection(task_mutation_tools) and "list_tasks" in exposed:
            return "list_tasks"

        for tool_name in project_context_required_read_tool_names(exposed):
            return tool_name
        return None

    @staticmethod
    def _pure_utility_request_kind(user_input: str) -> Optional[str]:
        """Classify one standalone utility request, excluding composites."""

        normalized = " ".join(str(user_input or "").casefold().split())
        if not normalized or any(
            marker in normalized for marker in UTILITY_COMPOSITE_ACTION_MARKERS
        ):
            return None
        kinds: set[str] = set()
        if any(
            marker in normalized
            for marker in (
                "今は何時",
                "今何時",
                "何時",
                "現在時刻",
                "現在の時刻",
                "現在の日時",
                "今の時間",
                "今日の日付",
                "what time",
                "current time",
                "current date",
                "current datetime",
                "get_current_time",
                "get current time",
            )
        ):
            kinds.add("time")
        if any(
            marker in normalized
            for marker in (
                "天気",
                "気温",
                "weather",
                "temperature",
                "get_weather_info",
                "get weather info",
                "get_weather",
            )
        ):
            kinds.add("weather")
        if any(
            marker in normalized
            for marker in ("計算", "電卓", "calculate", "calculator")
        ) or re.search(r"(?<!\w)\d+\s*[+\-*/%^×÷]\s*\d+", normalized):
            kinds.add("calculate")
        return next(iter(kinds)) if len(kinds) == 1 else None

    def _required_utility_tool_name(self, user_input: str) -> Optional[str]:
        """Select the deterministic utility tool for a direct utility request.

        Short time/weather/calculation questions should not depend on a local
        model deciding whether to call a tool from a large schema roster. The
        classifier is lexical and only narrows the already exposed root tools;
        it never grants a capability that is absent from the registry.
        """

        if (
            not getattr(self, "enable_tools", False)
            or (
                getattr(self, "_managed_local_runtime", None) != "strata"
                and getattr(self, "_profile_thinking_mode", None) != "always"
            )
            or not looks_like_utility_request(user_input)
        ):
            return None
        kind = self._pure_utility_request_kind(user_input)
        if kind == "time":
            candidates = ("get_current_time",)
        elif kind == "weather":
            candidates = ("get_weather_info",)
        elif kind == "calculate":
            candidates = ("calculate",)
        else:
            return None
        exposed = filtered_registry_for_client(self, self._tool_registry)
        for name in candidates:
            if name in exposed:
                return name
        return None

    def _project_context_read_already_satisfied(self) -> bool:
        """Return whether this request already has a successful grounding read.

        Only ``_last_audit_tool_calls`` is considered.  It is reset at the
        beginning of every ``generate_response`` request, so evidence from a
        prior turn cannot suppress the current turn's initial grounding.
        """

        required_reads = set(project_context_required_read_tool_names())
        if not required_reads:
            return False
        for record in getattr(self, "_last_audit_tool_calls", ()) or ():
            if isinstance(record, dict):
                tool_name = str(record.get("tool") or record.get("name") or "")
                explicit_success = record.get("successful", record.get("success"))
                result = record.get("result", record.get("output", ""))
            else:
                tool_name = str(
                    getattr(record, "tool", None)
                    or getattr(record, "name", None)
                    or ""
                )
                explicit_success = getattr(record, "successful", None)
                if explicit_success is None:
                    explicit_success = getattr(record, "success", None)
                result = getattr(record, "result", "")
            if tool_name not in required_reads:
                continue
            if isinstance(explicit_success, bool):
                if explicit_success:
                    return True
                continue
            # Older provider records may omit an explicit success flag.  Keep
            # the same conservative error-prefix semantics as the shared
            # record helpers rather than treating arbitrary failures as reads.
            lowered = str(result or "").strip().casefold()
            if not lowered.startswith(
                ("error:", "tool not found:", "tool execution error:")
            ):
                return True
        return False

    def _required_tool_name(self, user_input: str) -> Optional[str]:
        required = self._required_command_tool_name(
            user_input
        ) or self._required_project_context_read_tool_name(
            user_input
        ) or self._required_project_management_tool_name(user_input)
        if required:
            return required
        # Keep direct utility requests ahead of the generic task evidence
        # checks below.  A time question is read-only and has no project scope.
        required = self._required_utility_tool_name(user_input)
        if required:
            return required
        required_mutations = project_management_required_mutation_tools(
            user_input
        )
        if "create_task" in required_mutations and (
            successful_empty_task_search(
                getattr(self, "_last_audit_tool_calls", None)
            )
            is True
        ):
            exposed = filtered_registry_for_client(self, self._tool_registry)
            if "create_task" in exposed:
                return "create_task"
        return None

    def _stream_has_bound_agent_tools(self) -> bool:
        """Return whether a server-bound Agent turn has permitted native tools.

        The plain stream transport strips ``tools``/``tool_choice``.  A turn
        executing under this client's own Agent binding must keep the schemas
        its permissions exposed, so it goes through the native tool loop.
        Agent-less conversations keep the plain token stream.
        """

        turn = get_turn_context()
        binding = getattr(turn, "agent_execution_binding", None)
        if (
            binding is None
            or binding is not getattr(self, "_agent_execution_binding", None)
            or not str(getattr(turn, "agent_id", "") or "").strip()
        ):
            return False
        try:
            return bool(filtered_registry_for_client(self, self._tool_registry))
        except Exception:
            return False

    def _stream_should_use_tool_loop(
        self,
        user_input: str,
        required_tool_name: Optional[str],
    ) -> bool:
        """Use the native tool loop for retrieval-aware stream requests."""

        if not bool(getattr(self, "enable_tools", False)):
            return False
        if required_tool_name:
            return True
        if self._stream_has_bound_agent_tools():
            return True
        try:
            exposed = self._tool_registry_names()
        except Exception:
            exposed = set()
        if not {"media_search", "media_read"}.intersection(exposed):
            return False
        return _stream_request_needs_local_retrieval(user_input)

    def _build_context_bundle_sync(
        self,
        user_input: str,
        project_context: Optional[dict[str, Any]],
        context_budget: ContextBudget,
    ) -> Optional[ContextBundle]:
        self._last_memory_selection_observation = None
        if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            return None
        # Project Context is governed by the request-scoped flag (or the
        # provider compatibility flag), never by exact natural-language
        # phrases such as "検索してね".
        include_project_context = self._include_project_context_enabled()
        if (
            include_project_context
            and not project_context
            and not self.current_project_id
            and not self.current_session_id
        ):
            return None
        if not include_project_context and not self.current_session_id:
            return None
        from ..services.memory_selection_observation import collector_for_current_turn
        turn_task_id = get_turn_context().task_id
        collector = collector_for_current_turn(
            session_id=self.current_session_id, user_id=self._get_session_user_id(),
            project_id=str(self.current_project_id) if self.current_project_id and include_project_context else None,
            task_id=str(turn_task_id) if turn_task_id and include_project_context else None,
            stream_callback=_MEMORY_SELECTION_STREAM_CALLBACK.get(),
        )
        try:
            try:
                context_builder = ContextBuilder(manifest_config=self.config)
            except TypeError:
                # Keep compatibility with lightweight test/embedding doubles
                # that still expose the pre-WS1 zero-argument constructor.
                context_builder = ContextBuilder()
            bundle = self._run_async_sync(
                context_builder.build_context(
                    user_id=self._get_session_user_id(),
                    message=user_input,
                    project_id=self.current_project_id if include_project_context else None,
                    task_id=turn_task_id,
                    session_id=self.current_session_id,
                    project_context=project_context if include_project_context else None,
                    include_project_context=include_project_context,
                    max_chars=context_budget.context_bundle_chars,
                    **({"memory_selection_callback": collector.observe} if collector else {}),
                )
            )
            if collector:
                self._last_memory_selection_observation = collector.export()
            return bundle
        except Exception as exc:
            logger.warning(
                "[OpenAICompatibleLocalClient] ContextBuilder failed: %s",
                exc,
            )
            return None

    def _mode_extra_body(self) -> Dict[str, Any]:
        if getattr(self, "_managed_local_runtime", None) == "strata":
            mode = self._resolve_strata_mode()
            # Strata defaults to thinking when this template switch is absent.
            # Its fast mode must opt out on the first request, not after an
            # empty reasoning-only response has consumed the output budget.
            return {
                "chat_template_kwargs": {
                    "enable_thinking": mode == "thinking",
                },
            }
        if getattr(self, "_profile_thinking_mode", None) == "always":
            # The official LFM2.5 template emits ``<think>`` unconditionally
            # and has no ``enable_thinking`` switch.  Do not manufacture a
            # Qwen/Strata wire value for it.
            return {}
        metadata = self._reasoning_effort_metadata()
        if not metadata:
            return {}
        effort = str(self._current_llm_mode or "").strip().lower()
        if effort not in metadata["options"]:
            effort = str(metadata["default"])
        # The profile owns transport/path; malformed metadata fails closed
        # rather than silently reverting to a stale hard-coded wire shape.
        extra_body = llama_cpp_reasoning_effort_request_extra_body(
            self.model_name,
            effort,
        )
        if extra_body is None:
            raise ValueError(
                "Managed local profile has invalid reasoning effort wire metadata"
            )
        return extra_body

    def _profile_disables_thinking(self) -> bool:
        """Return whether the selected managed profile is non-reasoning.

        A profile capability of ``reasoning=false`` is stronger than generic
        local-server defaults.  Some llama.cpp Jinja templates otherwise
        leave their thinking channel implicit when no effort metadata exists,
        causing a non-reasoning chat profile such as Melody to spend its
        entire response budget on hidden planning text.
        """

        return getattr(self, "_profile_reasoning_supported", None) is False

    def _profile_sampling_defaults(self) -> Dict[str, Any]:
        """Return validated profile-owned sampling defaults for the request.

        LFM2.5 publishes temperature/top-k/repetition settings alongside its
        GGUF.  They are applied only for the always-thinking profile; generic
        local endpoints and existing managed profiles retain their historical
        request shape.  Caller-provided values in ``extra_body`` are merged
        after this baseline and therefore win.
        """

        if getattr(self, "_profile_thinking_mode", None) != "always":
            return {}
        profile = getattr(self, "_llama_cpp_profile", None)
        raw = profile.get("sampling_defaults") if isinstance(profile, dict) else None
        if not isinstance(raw, dict):
            return {}
        result: Dict[str, Any] = {}
        temperature = raw.get("temperature")
        try:
            temperature_value = float(temperature)
        except (TypeError, ValueError, OverflowError):
            temperature_value = float("nan")
        if math.isfinite(temperature_value) and 0.0 <= temperature_value <= 2.0:
            result["temperature"] = temperature_value
        top_k = raw.get("top_k")
        if isinstance(top_k, int) and not isinstance(top_k, bool) and top_k > 0:
            result["top_k"] = top_k
        repeat_penalty = raw.get("repeat_penalty")
        try:
            repeat_penalty_value = float(repeat_penalty)
        except (TypeError, ValueError, OverflowError):
            repeat_penalty_value = float("nan")
        if math.isfinite(repeat_penalty_value) and repeat_penalty_value > 0.0:
            result["repeat_penalty"] = repeat_penalty_value
        return result

    def _should_run_agentic_completion_review(self) -> bool:
        """Keep the generic review pass for capable profiles only.

        A known profile that advertises neither reasoning nor native tools has
        no actionable review/tool contract.  In the normal chat profile, and
        in an autonomous-work turn that has no command/tool requirement, a
        second full LLM request only makes short replies appear stuck.  Keep
        explicit assisted-work/review turns and work commands unchanged, as
        well as all unknown/Qwen profiles.
        """

        # Reserved Help is already grounded by the server-owned Guide prompt.
        # A second continuation/review request would be an unbounded provider
        # path and could reintroduce ordinary conversation context, so the
        # isolated turn never enters the generic agentic loop.
        if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            return False

        # A managed profile may explicitly opt out of the verifier for plain
        # chat without changing the shared GenerationProfile policy or the
        # behavior of other local profiles.  Keep this metadata separate from
        # reasoning/tool capability declarations: assisted, autonomous, and
        # review turns continue through the normal review contract.
        profile = get_client_generation_policy(self).profile
        runtime_profile = getattr(self, "_llama_cpp_profile", None)
        if runtime_profile is None and getattr(self, "_managed_local_runtime", None) == "strata":
            runtime_profile = strata_model_profile(self.model_name)
        current_command_capabilities = getattr(
            self, "current_command_capabilities", ()
        )
        if (
            profile == GenerationProfile.CHAT
            and isinstance(runtime_profile, dict)
            and runtime_profile.get("chat_agentic_completion_review_enabled") is False
            and not current_command_capabilities
            and getattr(self, "current_tool_required", None) is not True
        ):
            return False

        if not (
            self._profile_disables_thinking()
            and _llama_cpp_profile_capability(
                getattr(self, "_llama_cpp_profile", None),
                "tools",
            )
            is False
        ):
            return True

        if profile == GenerationProfile.CHAT:
            return False
        if profile != GenerationProfile.AUTONOMOUS_WORK:
            return True

        # The chat composer can retain ``autonomous_work`` from a previous
        # setting even when the current message is an ordinary prompt.  Do
        # not pay for a review that cannot execute tools in that case, while
        # preserving review for explicit command/tool turns.
        command_capabilities = getattr(self, "current_command_capabilities", ())
        if command_capabilities:
            return True
        return getattr(self, "current_tool_required", None) is True

    def _set_chat_template_thinking(
        self, api_kwargs: Dict[str, Any], enabled: bool
    ) -> Dict[str, Any]:
        retry_kwargs = dict(api_kwargs)
        extra_body = retry_kwargs.get("extra_body")
        if not isinstance(extra_body, dict):
            extra_body = {}
        if getattr(self, "_profile_thinking_mode", None) == "always":
            # Preserve the model-owned always-thinking contract even for
            # utility paths (memory/title retries) that historically asked
            # generic Jinja templates to disable thinking.
            extra_body = copy.deepcopy(extra_body)
            template_kwargs = extra_body.get("chat_template_kwargs")
            if isinstance(template_kwargs, dict):
                template_kwargs.pop("reasoning_effort", None)
                template_kwargs.pop("enable_thinking", None)
                if not template_kwargs:
                    extra_body.pop("chat_template_kwargs", None)
            extra_body.pop("reasoning_effort", None)
            extra_body.pop("enable_thinking", None)
            if extra_body:
                retry_kwargs["extra_body"] = extra_body
            else:
                retry_kwargs.pop("extra_body", None)
            return retry_kwargs
        retry_kwargs["extra_body"] = _deep_merge_dict(
            extra_body,
            {"chat_template_kwargs": {"enable_thinking": enabled}},
        )
        return retry_kwargs

    def _with_stream_safe_extra_body(self, api_kwargs: Dict[str, Any]) -> Dict[str, Any]:
        return api_kwargs

    def _payload_prompt_tokens(self, api_kwargs: Dict[str, Any]) -> int:
        return estimate_prompt_tokens(
            _compact_json(api_kwargs.get("messages") or [])
        ) + estimate_prompt_tokens(_compact_json(api_kwargs.get("tools") or []))

    def _tool_generation_max_tokens(
        self, api_kwargs: Dict[str, Any], budget: ContextBudget
    ) -> int:
        """Wire max_tokens for an uncapped tool/task request.

        The 1024-token response reserve only plans the prompt; using it as the
        wire cap truncated tool-code generation.  Without an explicit caller,
        runtime or planner cap, an operator-configured reserve or a curator
        override, allow up to the generic bounded allowance that still fits
        the actual messages + tool schemas in the context window.

        A legacy ``response_reserve_tokens`` stays a hard cap (it may be an
        intentional operator limit).  Only the explicit opt-in
        ``context_budget.tool_generation_max_tokens`` (positive int, default
        null) lets uncapped tool requests use the bounded allowance despite a
        legacy reserve; it is also the ceiling of that allowance.  A scoped
        curator override still wins.
        """

        current = int(api_kwargs.get("max_tokens") or budget.response_tokens)
        config = getattr(self, "config", None)
        getter = getattr(config, "get", None)
        if self._scoped_response_tokens_override() is not None:
            return current
        optin = None
        reserve = None
        if callable(getter):
            optin = getter(
                "openai_compatible_local.context_budget.tool_generation_max_tokens",
                None,
            )
            reserve = getter(
                "openai_compatible_local.context_budget.response_reserve_tokens",
                None,
            )
        try:
            optin = int(optin) if optin not in (None, "", False) else None
        except (TypeError, ValueError):
            optin = None
        if optin is not None and optin <= 0:
            optin = None
        if optin is None and reserve not in (None, ""):
            return current
        allowance = completion_allowance_tokens(
            budget,
            self._payload_prompt_tokens(api_kwargs),
            **({"ceiling": min(optin, TOOL_GENERATION_ALLOWANCE_TOKENS)} if optin else {}),
        )
        return allowance if allowance < current else max(current, allowance)

    def _bound_wire_max_tokens_to_window(
        self,
        api_kwargs: Dict[str, Any],
        context_budget: Optional[ContextBudget] = None,
    ) -> None:
        """Make messages + schemas + output + safety fit the window before a send.

        Lowers ``max_tokens`` to the space that really remains (never raises
        it, so explicit caller/runtime caps stay).  When the prompt itself
        leaves no usable completion the request is refused here rather than
        sent over the window.
        """

        max_tokens = api_kwargs.get("max_tokens")
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool):
            return
        budget = (
            context_budget
            or self._current_context_budget
            or self._context_budget()
        )
        available = completion_allowance_tokens(
            budget, self._payload_prompt_tokens(api_kwargs), ceiling=max_tokens
        )
        if available >= max_tokens:
            return
        if available < MIN_WIRE_COMPLETION_TOKENS:
            raise ValueError(
                "Local prompt (messages + tool schemas) leaves "
                f"{available} completion tokens in the "
                f"{budget.context_window_tokens}-token context window"
            )
        api_kwargs["max_tokens"] = available

    def _build_api_kwargs(
        self,
        messages: List[Dict[str, Any]],
        temperature: Optional[float],
        max_tokens: Optional[int],
        context_budget: Optional[ContextBudget] = None,
        tools_enabled: bool = True,
        required_tool_name: Optional[str] = None,
        native_tool_user_input: Optional[str] = None,
    ) -> Dict[str, Any]:
        if native_tool_user_input is not None:
            budget_user_input = native_tool_user_input
        else:
            budget_user_input = next(
                (
                    str(message.get("content") or "")
                    for message in reversed(messages)
                    if isinstance(message, dict)
                    and str(message.get("role") or "") == "user"
                ),
                "",
            )
        if max_tokens in (None, ""):
            max_tokens = self._configured_output_limit()
        projected_max_tokens = dialogue_provider_wire_max_output_tokens(
            self,
            user_input=budget_user_input,
            requested=max_tokens,
        )
        if projected_max_tokens is not None:
            if projected_max_tokens <= 0:
                raise ValueError("AgentDialogue provider budget is exhausted")
            max_tokens = projected_max_tokens
        uncapped_request = max_tokens in (None, "")
        budget = context_budget or self._context_budget(max_tokens)
        response_tokens = (
            min(max_tokens, budget.response_tokens)
            if max_tokens
            else budget.response_tokens
        )
        sampling_defaults = self._profile_sampling_defaults()
        request_temperature = 0.7 if temperature is None else temperature
        # ``None`` is the explicit provider-default sentinel.  A caller that
        # supplies 0.7 intentionally must retain it even when a profile's
        # recommended temperature differs.
        if temperature is None and "temperature" in sampling_defaults:
            request_temperature = sampling_defaults["temperature"]
        api_kwargs: Dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "temperature": request_temperature,
            "max_tokens": response_tokens,
        }
        suppress_automatic_context = bool(
            getattr(get_turn_context(), "suppress_automatic_context", False)
        )
        tools = (
            self._chat_completion_tools(
                context_budget=budget,
                required_tool_name=required_tool_name,
                user_input=native_tool_user_input,
            )
            if tools_enabled and not suppress_automatic_context
            else []
        )
        if required_tool_name and (
            self._is_llama_cpp_server()
            or required_tool_name in UTILITY_NATIVE_TOOL_NAMES
        ):
            narrowed_tools = [
                tool_def
                for tool_def in tools
                if str(getattr(tool_def, "name", "")) == required_tool_name
            ]
            if narrowed_tools:
                tools = narrowed_tools
            elif self._is_llama_cpp_server():
                raise LocalToolSelectionError(
                    f"必須ツール `{required_tool_name}` をこのリクエストに公開できません。"
                    "ツールの有効設定と利用可能なschemaを確認してください。"
                )
        if tools:
            api_kwargs["tools"] = self._native_tool_specs(tools, budget)
            omitted = tuple(getattr(self, "_omitted_native_tool_names", ()) or ())
            if omitted:
                self._note_omitted_native_tools(messages, omitted)
            available_names = {str(getattr(tool_def, "name", "")) for tool_def in tools}
            if required_tool_name and required_tool_name in available_names:
                if self._is_llama_cpp_server():
                    api_kwargs["tool_choice"] = "required"
                else:
                    api_kwargs["tool_choice"] = {
                        "type": "function",
                        "function": {"name": required_tool_name},
                    }
            else:
                api_kwargs["tool_choice"] = "auto"
        # Help answers are ordinary Guide-grounded prose.  A persisted JSON
        # response-format preference is normal-turn state and must not force a
        # schema-only response on this isolated path.
        if self.enable_response_format and not suppress_automatic_context:
            api_kwargs["response_format"] = {"type": "json_object"}
        extra_body: Dict[str, Any] = {
            key: value
            for key, value in sampling_defaults.items()
            if key != "temperature"
        }
        if self.server_profile.get("name") != "auto":
            profile_body = self.server_profile.get("request_extra_body")
            if isinstance(profile_body, dict) and profile_body:
                extra_body = _deep_merge_dict(extra_body, profile_body)
        if self.enable_extra_body and self.extra_body:
            extra_body = _deep_merge_dict(extra_body, self.extra_body)
        mode_extra_body = self._mode_extra_body()
        if mode_extra_body:
            extra_body = _deep_merge_dict(extra_body, mode_extra_body)
        if getattr(self, "_managed_local_runtime", None) == "strata":
            # The selected mode owns this wire. Stale effort fields can turn
            # Strata thinking back on/off independently of enable_thinking.
            extra_body["chat_template_kwargs"].pop("reasoning_effort", None)
            extra_body.pop("reasoning_effort", None)
            extra_body.pop("enable_thinking", None)
            extra_body.pop("reasoning", None)
        elif getattr(self, "_profile_thinking_mode", None) == "always":
            # LFM2.5's official Jinja template has no non-thinking branch.
            # Strip persisted generic/Qwen mode keys instead of sending an
            # unsupported ``enable_thinking=false`` or ``reasoning_effort``.
            template_kwargs = extra_body.get("chat_template_kwargs")
            if isinstance(template_kwargs, dict):
                template_kwargs.pop("reasoning_effort", None)
                template_kwargs.pop("enable_thinking", None)
                if not template_kwargs:
                    extra_body.pop("chat_template_kwargs", None)
            extra_body.pop("reasoning_effort", None)
            extra_body.pop("enable_thinking", None)
        elif self._reasoning_effort_metadata():
            # Profile-owned Qwen3.8 requests must use one canonical wire.  A
            # stale generic ``enable_thinking``/top-level effort from persisted
            # extra_body settings must not leak into the request projection.
            template_kwargs = extra_body.get("chat_template_kwargs")
            if isinstance(template_kwargs, dict):
                template_kwargs.pop("enable_thinking", None)
                effort = str(self._current_llm_mode or "").strip().lower()
                metadata = self._reasoning_effort_metadata() or {}
                if effort not in metadata.get("options", []):
                    effort = str(metadata.get("default") or "xhigh")
                template_kwargs["reasoning_effort"] = effort
            extra_body.pop("reasoning_effort", None)
            extra_body.pop("enable_thinking", None)
        elif self._profile_disables_thinking():
            # Melody and other explicitly non-reasoning profiles must opt out
            # of template thinking even when no effort wire is declared.  A
            # user-provided true value cannot override the profile contract.
            template_kwargs = extra_body.get("chat_template_kwargs")
            if not isinstance(template_kwargs, dict):
                template_kwargs = {}
                extra_body["chat_template_kwargs"] = template_kwargs
            template_kwargs.pop("reasoning_effort", None)
            template_kwargs["enable_thinking"] = False
            extra_body.pop("reasoning_effort", None)
            extra_body.pop("enable_thinking", None)
        if extra_body:
            api_kwargs["extra_body"] = extra_body
        self._required_llama_cpp_tool_request(api_kwargs)
        if suppress_automatic_context:
            self._cache_key = None
        else:
            self._cache_key = project_prompt_cache_key(
                user_id=self._get_session_user_id(),
                session_id=getattr(self, "current_session_id", None),
                project_id=(
                    self.current_project_id or get_turn_context().project_id
                ),
                character=self.character_name,
                model=self.model_name,
                system_prompt=self.system_prompt,
                tool_schemas=api_kwargs.get("tools", []),
                provider="openai_compatible_local",
                stable_context=getattr(self, "_current_prompt_stable_context", []),
                context_revision=getattr(self, "_current_project_cache_revision", ""),
                branch_fingerprint=str(getattr(self, "current_edit_message_id", None) or "default-branch"),
                summary_version=int(getattr(self.history_manager, "summary_version", 0) or 0),
                server_instance=str(self.session_metadata.get("server_instance") or "default-instance"),
            )
        config = getattr(self, "config", None)
        output_limit = config.get("runtime.target_max_output_tokens") if config is not None and hasattr(config, "get") else None
        generation_budget = current_generation_budget.get()
        # extra_body must never shadow the counted/fitted max_tokens, tools or
        # messages, whatever cap applies.
        api_kwargs = constrain_model_request_extra_body(api_kwargs)
        if uncapped_request and api_kwargs.get("tools"):
            api_kwargs["max_tokens"] = self._tool_generation_max_tokens(
                api_kwargs, budget
            )
        payload_cap = dialogue_provider_wire_max_output_tokens(
            self,
            user_input=budget_user_input,
            requested=int(api_kwargs.get("max_tokens") or 0),
            payload=api_kwargs,
        )
        if payload_cap is not None:
            if payload_cap <= 0:
                raise ValueError("AgentDialogue provider budget is exhausted")
            api_kwargs["max_tokens"] = min(
                int(api_kwargs.get("max_tokens") or payload_cap),
                payload_cap,
            )
        # Final bound against the request-local budget (never raises max_tokens,
        # so the post-fit follow-up cap above is not expanded).
        self._bound_wire_max_tokens_to_window(api_kwargs, budget)
        return api_kwargs

    def _chat_completion_tools(
        self,
        *,
        context_budget: Optional[ContextBudget] = None,
        required_tool_name: Optional[str] = None,
        user_input: Optional[str] = None,
    ) -> List[Any]:
        if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            return []
        if (
            not getattr(self, "enable_tools", False)
            or _llama_cpp_profile_capability(
                getattr(self, "_llama_cpp_profile", None),
                "tools",
            )
            is False
        ):
            return []
        if len(self._tool_registry) <= 0:
            return []

        budget = context_budget or self._current_context_budget
        compact_plain_chat = self._strata_plain_chat_schema_compaction_enabled()
        constrained = bool(budget) and (
            self._is_constrained_context_budget(budget) or compact_plain_chat
        )
        allowed_names: set[str] = set()
        hinted_names: set[str] = set()
        if constrained:
            allowed_names = self._constrained_native_tool_names(user_input)
            # A verified worker Dialogue may ask the Human even when another
            # trusted capability narrowed this request to a specialist pack.
            # Keep this additive only inside the canonical worker marker; an
            # ordinary/root local turn never receives the question schema here.
            if allowed_names and self._is_bound_worker_dialogue_turn():
                allowed_names.add("ask_user_question")
            if allowed_names and (
                self._is_bound_root_dialogue_turn()
                or self._is_bound_aoidots_root_turn()
            ):
                allowed_names.update(ROOT_DIALOGUE_NATIVE_TOOL_NAMES)
            if allowed_names:
                # 狭コンテキストで名指しした deferred ツールは、対応する pack を
                # 先にロードしてから実効集合を作る。ロード手段自体を失わないよう
                # `load_tool_pack` も常に許可名へ含める。
                auto_load_packs_for_tool_names(
                    tool_pack_session_for_client(self),
                    allowed_names,
                )
                allowed_names = {*allowed_names, LOAD_TOOL_PACK_TOOL_NAME}

        tools = filter_tools_for_client(self, self._tool_registry.get_all())
        agent_scope_names: set[str] = set()
        if isinstance(get_current_agent_tool_scope(), dict):
            # A bound AgentRevision ceiling denies every other tool at
            # execution.  Exposing those schemas only crowds the permitted
            # ones out of a constrained local schema budget, so narrow the
            # provider schemas to what the ceiling can allow.  Dialogue and
            # pack-loading controls keep their own execution gates.
            exempt = {
                *ROOT_DIALOGUE_NATIVE_TOOL_NAMES,
                "ask_user_question",
                LOAD_TOOL_PACK_TOOL_NAME,
            }
            narrowed = []
            for tool_def in tools:
                name = str(getattr(tool_def, "name", "") or "")
                if agent_tool_scope_permits_exposure(name, tool_def):
                    agent_scope_names.add(name)
                    narrowed.append(tool_def)
                elif name in exempt:
                    narrowed.append(tool_def)
            tools = narrowed
        if compact_plain_chat and not allowed_names:
            hinted_names = self._native_tool_hint_names(tools)
        if required_tool_name:
            if not any(
                str(getattr(tool_def, "name", "")) == required_tool_name
                for tool_def in tools
            ):
                logger.warning(
                    "[OpenAICompatibleLocalClient] Required native tool is unavailable: %s",
                    required_tool_name,
                )

        if not constrained:
            if budget is None:
                return tools
            return self._fit_native_tools_to_token_budget(
                tools,
                budget,
                required_tool_name=required_tool_name,
            )

        if not allowed_names:
            # No explicit capability selector chose a hard domain.  Hints only
            # affect priority here; keep the remaining filtered tools eligible
            # so a composite Docs/workspace request is not made tool-blind.
            # Deferred specialist packs remain represented by `load_tool_pack`.
            return self._fit_native_tools_to_schema_budget(
                tools,
                budget,
                required_tool_name=required_tool_name,
                priority_tool_names=_ordered_scope_priority(hinted_names, agent_scope_names),
                mandatory_tool_names=agent_scope_names & set(WORK_LOOP_NATIVE_TOOL_ORDER),
            )
        selected = [
            tool_def
            for tool_def in tools
            if str(getattr(tool_def, "name", "")) in allowed_names
        ]
        if required_tool_name and not any(
            str(getattr(tool_def, "name", "")) == required_tool_name
            for tool_def in selected
        ):
            selected = [
                tool_def
                for tool_def in tools
                if str(getattr(tool_def, "name", "")) == required_tool_name
            ] + selected
        return self._fit_native_tools_to_schema_budget(
            selected,
            budget,
            required_tool_name=required_tool_name,
            priority_tool_names=_ordered_scope_priority(set(), allowed_names),
        )

    def _strata_plain_chat_schema_compaction_enabled(self) -> bool:
        """Compact Strata's ordinary chat schemas without hiding work tools.

        Strata's 32k context is large enough to enter the legacy all-schema
        path, but ordinary chat does not need the full registry.  The turn
        context still carries explicit command capabilities and
        ``current_tool_required``; those paths remain available below.  Other
        providers keep their historical schema behavior.
        """
        return (
            getattr(self, "_managed_local_runtime", None) == "strata"
            and get_client_generation_policy(self).profile == GenerationProfile.CHAT
            and getattr(self, "current_tool_required", None) is not True
        )

    def _native_tool_hint_names(self, tools: List[Any]) -> set[str]:
        """Return provider-visible tool names from trusted generated hints.

        ``_current_tool_hint_context`` is built by ``build_tool_hint_context``
        from the filtered registry; it is not raw user prose.  Restricting the
        names to this request's filtered definitions preserves the exposure
        boundary while keeping Docs/past-chat/media routing available.
        """
        available = {
            str(getattr(tool_def, "name", "") or "") for tool_def in tools
        }
        hinted = {
            name
            for name in re.findall(
                r"`([A-Za-z_][A-Za-z0-9_]*)`",
                str(getattr(self, "_current_tool_hint_context", "") or ""),
            )
            if name in available
        }
        if "media_search" in hinted and "media_read" in available:
            hinted.add("media_read")
        return hinted

    def _constrained_native_tool_names(
        self,
        user_input: Optional[str],
    ) -> set[str]:
        text = str(user_input or "")
        if not text.strip():
            return set()

        names: set[str] = set()
        explicit_capabilities = set(
            sanitize_command_capabilities(
                getattr(self, "current_command_capabilities", ()) or ()
            )
        ) | command_capabilities_from_text(text)

        # Only trusted command capabilities and server-verified references
        # constrain a narrow schema.  Natural Docs/Files/Project/utility/media
        # wording must not hide the other tools from the model.
        if "web_search" in explicit_capabilities:
            names.add("web_search")
        if "project_progress_review" in explicit_capabilities:
            names.update(DIRECT_PROJECT_TOOL_HINT_NAMES)
        if "project_db_update" in explicit_capabilities:
            names.update(
                {
                    "organize_project_information_from_folder",
                    "patch_project_information_doc",
                    "attach_project_information_reference",
                }
            )
            names.update(
                {
                    "get_project_context",
                    "list_project_information",
                    "list_record_tables",
                }
            )
        if "task_update" in explicit_capabilities:
            names.update({"list_tasks", "create_task", "update_task"})
        if "wbs_sync" in explicit_capabilities:
            names.update({"sync_wbs_tasks", "get_upcoming_wbs_tasks", "list_tasks"})
        if "image_generation" in explicit_capabilities:
            names.add("media_assistant")
        if "workspace_file_operation" in explicit_capabilities:
            names.update(MANAGED_WORKSPACE_NATIVE_TOOL_NAMES)
        if looks_like_managed_workspace_request(text):
            # ``looks_like_managed_workspace_request`` is true only for the
            # trusted command capability or the server-set
            # ``TurnContext.verified_project_attachment`` flag.  Raw prompt
            # markers and ordinary workspace wording do not reach this path.
            names.update(MANAGED_WORKSPACE_NATIVE_TOOL_NAMES)
        return names

    def _fit_native_tools_to_schema_budget(
        self,
        tools: List[Any],
        context_budget: ContextBudget,
        *,
        required_tool_name: Optional[str] = None,
        priority_tool_names: Optional[List[str]] = None,
        mandatory_tool_names: Optional[Iterable[str]] = None,
    ) -> List[Any]:
        """Select tools whose schemas fit the shared schema token budget.

        The allowance is the same input-token share the unconstrained path
        uses, so schema and message budgets come from one ``ContextBudget``.
        Descriptions are compacted level by level before any tool is left
        out.  Mandatory tools (required, dialogue controls and the permitted
        work-loop core) are never left out, even when they alone exceed the
        allowance; only optional tools are, and the names are recorded in
        ``_omitted_native_tool_names`` so the request can say so.
        """
        self._omitted_native_tool_names = ()
        if len(tools) <= 1:
            return tools
        required_name = str(required_tool_name or "").strip()
        if self._is_bound_root_dialogue_turn() or self._is_bound_aoidots_root_turn():
            dialogue_priority = ROOT_DIALOGUE_NATIVE_TOOL_NAMES
        elif self._is_bound_worker_dialogue_turn():
            dialogue_priority = ("ask_user_question",)
        else:
            dialogue_priority = ()
        priority = [
            name
            for name in (
                required_name,
                *dialogue_priority,
                *(priority_tool_names or []),
                *CONSTRAINED_PRIORITY_NATIVE_TOOL_NAMES,
            )
            if name
        ]
        rank = {name: index for index, name in enumerate(dict.fromkeys(priority))}
        # 安定ソートで優先ツールだけ前へ出し、残りはregistry順を保つ。
        ordered_tools = sorted(
            tools,
            key=lambda tool_def: rank.get(
                str(getattr(tool_def, "name", "") or ""), len(rank)
            ),
        )
        mandatory = {
            name
            for name in (
                required_name,
                *dialogue_priority,
                *(mandatory_tool_names or ()),
            )
            if name
        }
        allowance = max(900, self._native_tool_schema_token_budget(context_budget))
        specs = {
            id(tool_def): OpenAIAPIAdapter.convert(tool_def) for tool_def in ordered_tools
        }
        selected: List[Any] = []
        # Ordinary chat has no work-loop core to protect: keep its schemas whole and
        # leave out what does not fit.  Work loops compact descriptions first.
        levels = NATIVE_TOOL_SCHEMA_COMPACTION_LEVELS if mandatory else NATIVE_TOOL_SCHEMA_COMPACTION_LEVELS[:1]
        for description_chars, parameter_chars in levels:
            sizes = {
                key: estimate_prompt_tokens(
                    _compact_json(compact_native_tool_spec(spec, description_chars, parameter_chars))
                )
                + 1
                for key, spec in specs.items()
            }
            used = 2
            selected = []
            for tool_def in ordered_tools:
                size = sizes[id(tool_def)]
                is_mandatory = str(getattr(tool_def, "name", "") or "") in mandatory
                if used + size <= allowance or is_mandatory or not selected:
                    selected.append(tool_def)
                    used += size
            if len(selected) == len(ordered_tools):
                return selected
        omitted = tuple(
            str(getattr(tool_def, "name", "") or "")
            for tool_def in ordered_tools
            if tool_def not in selected
        )
        self._omitted_native_tool_names = omitted
        logger.warning(
            "[OpenAICompatibleLocalClient] Left out optional native tool schemas over the %s-token schema budget: %s",
            allowance,
            ", ".join(omitted),
        )
        return selected

    @staticmethod
    def _note_omitted_native_tools(messages: List[Any], omitted: Iterable[str]) -> None:
        """Tell the model which permitted tools were left out of this request.

        The schema budget is shared with the messages, so optional tools can
        be absent.  Say so explicitly instead of letting the model guess; the
        work-loop core is never among them.
        """
        note = (
            "\n\n[このリクエストの送信予算の都合で、次のツールのschemaは含まれていません: "
            + ", ".join(omitted)
            + "。これらが必要な場合は、使えるツールで進められる範囲を進め、"
            "必要なツール名を最終回答に明記してください。]"
        )
        for index, message in enumerate(messages):
            if isinstance(message, dict) and message.get("role") == "system":
                content = message.get("content")
                if isinstance(content, str):
                    messages[index] = {**message, "content": content + note}
                    return

    def _tool_registry_names(self) -> set[str]:
        registry = getattr(self, "_tool_registry", None)
        try:
            return {str(getattr(tool_def, "name", "") or "") for tool_def in registry.get_all()}
        except Exception:
            return set()

    def _native_tool_schema_token_budget(self, context_budget: ContextBudget) -> int:
        return max(1, int(context_budget.input_tokens * NATIVE_TOOL_SCHEMA_INPUT_TOKEN_SHARE))

    def _is_bound_root_dialogue_turn(self) -> bool:
        """Return whether this request has the exact Root Dialogue marker."""

        turn = get_turn_context()
        if (
            str(getattr(turn, "agent_dialogue_role", "") or "").strip().casefold()
            != "root"
            or not str(getattr(turn, "agent_dialogue_id", "") or "").strip()
            or not str(getattr(turn, "session_id", "") or "").strip()
            or not str(getattr(turn, "agent_id", "") or "").strip()
            or not str(getattr(turn, "agent_revision_id", "") or "").strip()
        ):
            return False
        binding = getattr(turn, "agent_execution_binding", None)
        if binding is None:
            return False

        def binding_value(name: str) -> Any:
            if isinstance(binding, Mapping):
                return binding.get(name)
            return getattr(binding, name, None)

        if any(
            str(binding_value(field) or "").strip()
            != str(getattr(turn, context_field, "") or "").strip()
            for field, context_field in (
                ("session_id", "session_id"),
                ("agent_id", "agent_id"),
                ("agent_revision_id", "agent_revision_id"),
            )
        ):
            return False
        marker = getattr(turn, "agent_execution_config", None)
        if not isinstance(marker, Mapping):
            marker = (
                binding.get("_root_scope_marker")
                if isinstance(binding, Mapping)
                else getattr(binding, "_root_scope_marker", None)
            )
        if not isinstance(marker, Mapping):
            return False
        kind = str(
            marker.get("scope_kind")
            or marker.get("conversation_kind")
            or marker.get("dialogue_kind")
            or ""
        ).strip().casefold()
        role = str(
            marker.get("dialogue_role") or marker.get("role") or ""
        ).strip().casefold()
        if kind != "agent_dialogue" or role != "root":
            return False
        if str(
            marker.get("agent_dialogue_id") or marker.get("dialogue_id") or ""
        ).strip() != str(turn.agent_dialogue_id).strip():
            return False
        marker_identity = (
            ("root_session_id", "dialogue_session_id", "session_id"),
            ("root_agent_id", "dialogue_agent_id", "agent_id"),
            (
                "root_agent_revision_id",
                "dialogue_agent_revision_id",
                "agent_revision_id",
            ),
        )
        for *marker_fields, context_field in marker_identity:
            marker_value = str(
                next((marker.get(field) for field in marker_fields if marker.get(field)), "")
            ).strip()
            if not marker_value or marker_value != str(
                getattr(turn, context_field, "") or ""
            ).strip():
                return False
        return True

    def _is_bound_aoidots_root_turn(self) -> bool:
        """Return whether this is the server-bound initial AoiDots Root turn."""

        turn = get_turn_context()
        if (
            str(getattr(turn, "project_id", "") or "").strip()
            or str(getattr(turn, "agent_dialogue_id", "") or "").strip()
            or str(getattr(turn, "agent_dialogue_role", "") or "").strip()
        ):
            return False
        binding = getattr(turn, "agent_execution_binding", None)
        if binding is None or binding is not getattr(self, "_agent_execution_binding", None):
            return False
        try:
            from ..services.chat_execution_adapter import InitialAoiDotsRootProof
            from ..services.agent_run_service import get_current_agent_run_id

            proof = getattr(binding, "_initial_aoidots_root_proof", None)
            current_run_id = get_current_agent_run_id()
        except Exception:
            return False
        if not isinstance(proof, InitialAoiDotsRootProof) or not current_run_id:
            return False
        binding_marker = getattr(binding, "_root_scope_marker", None)
        if not isinstance(binding_marker, Mapping) or dict(binding_marker) != proof.marker():
            return False
        return all(
            (
                str(getattr(turn, "user_id", "") or "") == proof.owner_user_id,
                str(getattr(turn, "session_id", "") or "") == proof.session_id,
                str(getattr(turn, "agent_id", "") or "") == proof.agent_id,
                str(getattr(turn, "agent_revision_id", "") or "")
                == proof.agent_revision_id,
                str(current_run_id) == proof.run_id,
                str(getattr(binding, "agent_id", "") or "") == proof.agent_id,
                str(getattr(binding, "session_id", "") or "") == proof.session_id,
                str(getattr(binding, "agent_revision_id", "") or "")
                == proof.agent_revision_id,
            )
        )

    def _is_bound_worker_dialogue_turn(self) -> bool:
        """Return whether this request has the server-bound worker marker.

        A model-visible role string is insufficient to expose the question
        schema. Require the immutable Dialogue marker plus the current
        session/Agent identity so ordinary local turns and forged worker-like
        config values retain the historical tool priority.
        """

        turn = get_turn_context()
        if (
            str(getattr(turn, "agent_dialogue_role", "") or "").strip().casefold()
            != "worker"
            or not str(getattr(turn, "agent_dialogue_id", "") or "").strip()
            or not str(getattr(turn, "user_id", "") or "").strip()
            or not str(getattr(turn, "session_id", "") or "").strip()
            or not str(getattr(turn, "agent_id", "") or "").strip()
            or not str(getattr(turn, "agent_revision_id", "") or "").strip()
            or getattr(turn, "agent_execution_binding", None) is None
        ):
            return False
        marker = getattr(turn, "agent_execution_config", None)
        if not isinstance(marker, Mapping):
            binding = getattr(turn, "agent_execution_binding", None)
            marker = getattr(binding, "_root_scope_marker", None)
        if not isinstance(marker, Mapping):
            return False
        kind = str(
            marker.get("scope_kind")
            or marker.get("conversation_kind")
            or marker.get("dialogue_kind")
            or ""
        ).strip().casefold()
        role = str(
            marker.get("dialogue_role") or marker.get("role") or ""
        ).strip().casefold()
        marker_dialogue_id = str(
            marker.get("agent_dialogue_id") or marker.get("dialogue_id") or ""
        ).strip()
        if kind != "agent_dialogue" or role != "worker":
            return False
        if marker_dialogue_id != str(turn.agent_dialogue_id).strip():
            return False
        for marker_key, context_value in (
            ("worker_session_id", turn.session_id),
            ("worker_agent_id", turn.agent_id),
            ("worker_agent_revision_id", turn.agent_revision_id),
        ):
            marker_value = str(marker.get(marker_key) or "").strip()
            if marker_value and marker_value != str(context_value).strip():
                return False
        return True

    def _native_tool_priority_names(self, required_tool_name: Optional[str]) -> list[str]:
        """Tools kept first when the schema budget forces leaving some out."""
        hinted = re.findall(
            r"`([A-Za-z_][A-Za-z0-9_]*)`",
            str(getattr(self, "_current_tool_hint_context", "") or ""),
        )
        dialogue_priority = (
            ROOT_DIALOGUE_NATIVE_TOOL_NAMES
            if (
                self._is_bound_root_dialogue_turn()
                or self._is_bound_aoidots_root_turn()
            )
            else ("ask_user_question",)
            if self._is_bound_worker_dialogue_turn()
            else ()
        )
        names = (
            str(required_tool_name or "").strip(),
            *hinted,
            *dialogue_priority,
            LOAD_TOOL_PACK_TOOL_NAME,
            *CONSTRAINED_PRIORITY_NATIVE_TOOL_NAMES,
        )
        return list(dict.fromkeys(name for name in names if name))

    def _fit_native_tools_to_token_budget(
        self,
        tools: List[Any],
        context_budget: ContextBudget,
        *,
        required_tool_name: Optional[str] = None,
    ) -> List[Any]:
        """Keep the schema within its input-token share above the constrained window.

        Descriptions are shortened first (see ``_native_tool_specs``); tools are
        left out only when even the most compact schemas exceed the share, and
        then the required/hinted/pack-loader/local-search tools are kept first.
        """
        if len(tools) <= 1:
            return tools
        budget_tokens = self._native_tool_schema_token_budget(context_budget)
        specs = [OpenAIAPIAdapter.convert(tool_def) for tool_def in tools]
        if estimate_prompt_tokens(_compact_json(specs)) <= budget_tokens:
            return tools
        most_compact = NATIVE_TOOL_SCHEMA_COMPACTION_LEVELS[-1]
        compact_specs = [compact_native_tool_spec(spec, *most_compact) for spec in specs]
        if estimate_prompt_tokens(_compact_json(compact_specs)) <= budget_tokens:
            return tools
        ordered = _native_tool_keep_order(
            tools,
            self._native_tool_priority_names(required_tool_name),
        )
        kept: set[int] = set()
        used = 2
        for index in ordered:
            size = estimate_prompt_tokens(_compact_json(compact_specs[index])) + 1
            if used + size <= budget_tokens or not kept:
                kept.add(index)
                used += size
        selected = [tool_def for index, tool_def in enumerate(tools) if index in kept]
        logger.warning(
            "[OpenAICompatibleLocalClient] Left out native tool schemas over the %s-token schema budget: %s -> %s",
            budget_tokens,
            len(tools),
            len(selected),
        )
        return selected

    def _native_tool_specs(
        self,
        tools: List[Any],
        context_budget: Optional[ContextBudget],
    ) -> List[Dict[str, Any]]:
        """Convert tools, shortening descriptions only as far as the budget needs."""
        specs = OpenAIAPIAdapter.convert_all(tools)
        if context_budget is None:
            return specs
        budget_tokens = self._native_tool_schema_token_budget(context_budget)
        candidate = specs
        for description_chars, parameter_chars in NATIVE_TOOL_SCHEMA_COMPACTION_LEVELS:
            candidate = [
                compact_native_tool_spec(spec, description_chars, parameter_chars)
                for spec in specs
            ]
            if estimate_prompt_tokens(_compact_json(candidate)) <= budget_tokens:
                break
        return candidate

    def _mode_extra_body_from_kwargs(
        self, api_kwargs: Dict[str, Any]
    ) -> Dict[str, Any]:
        if getattr(self, "_managed_local_runtime", None) == "strata":
            # A final bounded retry must not reuse a stale mode wire after the
            # live adapter state has become invalid.
            self._resolve_strata_mode()
        if getattr(self, "_profile_thinking_mode", None) == "always":
            # Preserve caller-owned sampling/vendor extras on a compatibility
            # retry.  Only the generic mode switches are removed; rebuilding
            # the profile baseline here would overwrite explicit values such
            # as top_k/repeat_penalty.
            extra_body = api_kwargs.get("extra_body")
            if not isinstance(extra_body, dict):
                return {}
            preserved = copy.deepcopy(extra_body)
            template_kwargs = preserved.get("chat_template_kwargs")
            if isinstance(template_kwargs, dict):
                template_kwargs.pop("reasoning_effort", None)
                template_kwargs.pop("enable_thinking", None)
                if not template_kwargs:
                    preserved.pop("chat_template_kwargs", None)
            preserved.pop("reasoning_effort", None)
            preserved.pop("enable_thinking", None)
            return preserved
        extra_body = api_kwargs.get("extra_body")
        if not isinstance(extra_body, dict):
            return {}
        template_kwargs = extra_body.get("chat_template_kwargs")
        if not isinstance(template_kwargs, dict):
            return {}
        keys = {
            key: template_kwargs[key]
            for key in ("reasoning_effort", "enable_thinking")
            if key in template_kwargs
        }
        if not keys:
            return {}
        return {"chat_template_kwargs": keys}

    def _compatibility_retry_kwargs(
        self, api_kwargs: Dict[str, Any]
    ) -> tuple[Dict[str, Any], list[str]]:
        strata_final_no_tools = (
            getattr(self, "_managed_local_runtime", None) == "strata"
            and (
                not api_kwargs.get("tools")
                or api_kwargs.get("tool_choice") == "none"
            )
        )
        if getattr(self, "_managed_local_runtime", None) == "strata":
            # Compatibility retries must use the same live mode resolver as
            # the first request; never resurrect a stale private mode here.
            self._resolve_strata_mode()
        retry_kwargs = dict(api_kwargs)
        removed = []
        removable = (
            ("response_format",)
            if self._required_llama_cpp_tool_request(api_kwargs)
            else ("tools", "tool_choice", "response_format")
        )
        for key in removable:
            if key in retry_kwargs:
                retry_kwargs.pop(key, None)
                removed.append(key)
        if "extra_body" in retry_kwargs:
            if strata_final_no_tools:
                raw_extra_body = retry_kwargs.get("extra_body")
                mode_extra_body = (
                    dict(raw_extra_body)
                    if isinstance(raw_extra_body, dict)
                    else {}
                )
                for selector in (
                    "tools",
                    "tool_choice",
                    "functions",
                    "function_call",
                ):
                    mode_extra_body.pop(selector, None)
            else:
                mode_extra_body = (
                    self._mode_extra_body()
                    if self._reasoning_effort_metadata()
                    else self._mode_extra_body_from_kwargs(api_kwargs)
                )
            if mode_extra_body:
                if retry_kwargs["extra_body"] != mode_extra_body:
                    retry_kwargs["extra_body"] = mode_extra_body
                    removed.append("extra_body(non-mode)")
            else:
                retry_kwargs.pop("extra_body", None)
                removed.append("extra_body")
        if strata_final_no_tools:
            self._enforce_strata_final_answer_contract(retry_kwargs)
        return retry_kwargs, removed

    def _enforce_strata_final_answer_contract(
        self, api_kwargs: Dict[str, Any]
    ) -> None:
        """Keep Strata no-tool requests explicit without dropping mode extras."""

        if (
            getattr(self, "_managed_local_runtime", None) != "strata"
            or (
                api_kwargs.get("tools")
                and api_kwargs.get("tool_choice") != "none"
            )
        ):
            return
        api_kwargs["tool_choice"] = "none"
        for selector in ("functions", "function_call"):
            api_kwargs.pop(selector, None)
        extra_body = api_kwargs.get("extra_body")
        if not isinstance(extra_body, dict):
            return
        extra_body = dict(extra_body)
        api_kwargs["extra_body"] = extra_body
        for selector in ("tools", "tool_choice", "functions", "function_call"):
            extra_body.pop(selector, None)
        if not extra_body:
            api_kwargs.pop("extra_body", None)

    def _wait_for_local_server_health(self, *, timeout_seconds: float = 30.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while time.monotonic() < deadline:
            if self.health_check().get("ok"):
                return True
            time.sleep(0.5)
        return False

    def _served_local_model_ids(self) -> set[str]:
        served: set[str] = set()
        try:
            from src.service_manager import (
                _llama_cpp_model_ids_exact,
                _local_openai_model_ids,
            )

            served = {
                str(item).strip()
                for item in (_llama_cpp_model_ids_exact(self.base_url) or set())
                if str(item).strip()
            }
            if not served:
                served = {
                    str(item).strip()
                    for item in (_local_openai_model_ids(self.base_url) or set())
                    if str(item).strip()
                }
        except Exception:
            served = set()
        if served:
            return served
        try:
            return {
                str(item.get("id") or "").strip()
                for item in self.list_models()
                if isinstance(item, dict) and str(item.get("id") or "").strip()
            }
        except Exception:
            return set()

    def _expected_local_model_is_served(self) -> bool:
        expected = str(self.model_name or "").strip()
        if not expected:
            return False
        return expected in self._served_local_model_ids()

    def _local_runtime_consumer_lease_lock_for_client(self) -> threading.RLock:
        """Return the client-local fence for consumer lease transitions.

        A few lightweight test/embedding adapters instantiate this class with
        ``__new__`` and only populate the fields they exercise.  Lazily
        creating the lock keeps those adapters compatible while normal
        clients initialize it in ``__init__``.
        """

        lock = getattr(self, "_llama_cpp_runtime_consumer_lease_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._llama_cpp_runtime_consumer_lease_lock = lock
        return lock

    def _release_managed_local_generation_lease(self, *, all: bool = False) -> None:
        try:
            from src.service_manager import _release_llama_cpp_generation_lease_for_holder
        except Exception:
            return
        _release_llama_cpp_generation_lease_for_holder(self, all=all)

    def _acquire_managed_local_runtime_consumer_lease(self) -> None:
        """Register a target client that owns a managed local runtime.

        Only explicit target consumers opt in through
        ``runtime.local_runtime_consumer``.  The long-lived global client
        therefore remains switchable, while session/group/meeting/agent
        clients keep their endpoint alive until their cleanup hook runs.
        """

        with self._local_runtime_consumer_lease_lock_for_client():
            # A retired cache entry may still finish a title/tool round.  Its
            # generation lease remains request-owned, but it must never regain
            # persistent consumer retention while that work re-enters the
            # provider preparation path.
            if getattr(self, "_local_runtime_consumer_lease_retired", False):
                return
            consumer = str(
                _config_get(self.config, "runtime.local_runtime_consumer", "") or ""
            ).strip()
            if not consumer or self._managed_local_runtime not in {
                "llama_cpp",
                "freetoken",
                "strata",
            }:
                return
            try:
                from src.service_manager import (
                    _acquire_managed_local_runtime_consumer_for_route,
                )
                ticket = _acquire_managed_local_runtime_consumer_for_route(
                    self.config,
                    provider="openai_compatible_local",
                    model=self.model_name,
                    consumer=consumer,
                    holder=self,
                )
                if ticket is not None:
                    self._llama_cpp_runtime_consumer_lease_tickets.add(ticket)
            except Exception:
                # A consumer lease is a lifecycle safety boundary.  If an active
                # target conflicts with another alias, fail target construction so
                # the switch cannot silently create a client that would later be
                # force-killed.  Missing identity/runtime helpers remain a no-op
                # for lightweight legacy test doubles.
                raise

    def _release_managed_local_runtime_consumer_lease(self, *, all: bool = False) -> None:
        with self._local_runtime_consumer_lease_lock_for_client():
            try:
                from src.service_manager import (
                    _release_llama_cpp_runtime_consumer_leases_for_holder,
                )
            except Exception:
                return
            _release_llama_cpp_runtime_consumer_leases_for_holder(self, all=all)

    def _retire_managed_local_runtime_consumer_lease(self) -> bool:
        """Retire this cached client without interrupting active generation.

        The response-model cache uses this boundary when it drops a client.
        It releases only consumer tickets owned by this client; SDK state and
        request generation tickets remain untouched.  A retired client may
        still finish an in-flight stream, title, or tool round.  Its next
        provider preparation can therefore acquire a request-scoped
        generation ticket, while ``_acquire_managed_local_runtime_consumer_lease``
        refuses to recreate persistent retention.  Repeated calls are safe;
        the service-manager holder release and each ticket release are
        idempotent.  A failed release leaves the completion marker unset so a
        later retirement attempt can retry the owner-scoped cleanup.

        Returns ``True`` only when this call changes the client from active to
        retired, and ``False`` for an already-retired client.
        """

        with self._local_runtime_consumer_lease_lock_for_client():
            already_retired = bool(
                getattr(self, "_local_runtime_consumer_lease_retired", False)
            )
            self._local_runtime_consumer_lease_retired = True
            if not getattr(
                self,
                "_local_runtime_consumer_lease_retire_release_complete",
                False,
            ):
                self._release_managed_local_runtime_consumer_lease(all=True)
                self._local_runtime_consumer_lease_retire_release_complete = True
            return not already_retired

    def _release_requested_local_runtime_consumer_lease(self) -> None:
        if not self._local_runtime_consumer_lease_release_requested:
            return
        if any(
            not event.is_set()
            for event in getattr(self, "_ephemeral_workers", ())
        ):
            return
        self._local_runtime_consumer_lease_release_requested = False
        self._release_managed_local_runtime_consumer_lease(all=True)

    def _ensure_managed_local_server_ready(
        self,
        *,
        acquire_generation_lease: bool = False,
        lease_tickets: list[Any] | None = None,
    ) -> bool:
        if not self._can_attempt_managed_llama_cpp_recovery():
            config_error = self._managed_llama_cpp_launch_configuration_error()
            if config_error:
                self._last_local_server_ensure_error = config_error
                self._mark_runtime_unavailable(config_error)
            elif self._is_manual_managed_llama_cpp_runtime():
                message = (
                    "手動接続先の llama-server に接続できません。"
                    " endpoint が起動しているか確認してください。"
                )
                self._last_local_server_ensure_error = message
                self._mark_runtime_unavailable(message)
            return False
        if not self.config:
            return False
        self._last_local_server_ensure_error = None
        ticket = None
        try:
            from src.service_manager import ensure_openai_compatible_local_server

            acquired: list[Any] = []
            try:
                ensure_openai_compatible_local_server(
                    self.config,
                    raise_on_launch_error=True,
                    force_restart=False,
                    model=self.model_name,
                    acquire_generation_lease=acquire_generation_lease,
                    lease_holder=self if acquire_generation_lease else None,
                    lease_tickets=acquired if acquire_generation_lease else None,
                )
            except TypeError as exc:
                # Keep lightweight test/dry-run adapters which implement the
                # pre-lease ensure signature working; the production service
                # manager accepts the request-scoped lease keywords above.
                if "acquire_generation_lease" not in str(exc):
                    raise
                ensure_openai_compatible_local_server(
                    self.config,
                    raise_on_launch_error=True,
                    force_restart=False,
                    model=self.model_name,
                )
            ticket = acquired[0] if acquired else None
            if self._expected_local_model_is_served():
                self._clear_runtime_unavailability()
                # Acquire the long-lived consumer lease only after the
                # lifecycle ensure has selected/stabilized the endpoint. Doing
                # this in __init__ can self-deadlock a session target that is
                # switching aliases on an already-owned port.
                self._acquire_managed_local_runtime_consumer_lease()
                if ticket is not None and lease_tickets is not None:
                    lease_tickets.append(ticket)
                elif ticket is not None and lease_tickets is None:
                    ticket.release()
                return True
        except Exception as exc:
            if ticket is not None:
                ticket.release()
            self._last_local_server_ensure_error = str(exc)
            logger.warning(
                "[OpenAICompatibleLocalClient] Managed local server ensure failed: %s",
                exc,
            )
            self._mark_runtime_unavailable(str(exc))
            return False
        if ticket is not None:
            ticket.release()
        message = (
            "llama-server は応答していますが、"
            f"期待alias {str(self.model_name or '').strip()!r} が /v1/models にありません。"
        )
        self._last_local_server_ensure_error = message
        self._mark_runtime_unavailable(message)
        return False

    @staticmethod
    def _current_agent_run_id_for_preprovider_proof() -> str | None:
        try:
            from ..services.agent_run_service import get_current_agent_run_id

            value = get_current_agent_run_id()
        except Exception:
            return None
        if value in (None, ""):
            return None
        normalized = str(value).strip()
        return normalized or None

    def _wrap_preprovider_readiness_failure(
        self,
        error: BaseException,
        *,
        request_type: str,
    ) -> BaseException:
        """Classify only a budgeted, zero-sender readiness failure."""

        if not isinstance(error, ConnectionError):
            return error
        if _LOCAL_PROVIDER_SENDER_ATTEMPTS.get() != 0:
            return error
        if not isinstance(getattr(self, "_agent_dialogue_budget", None), Mapping):
            return error
        run_id = self._current_agent_run_id_for_preprovider_proof()
        if not run_id:
            return error
        return LocalProviderNotStartedError(
            request_type=request_type,
            run_id=run_id,
        )

    def _prepare_managed_local_server_for_request(self) -> Any:
        if self._can_attempt_managed_llama_cpp_recovery():
            tickets: list[Any] = []
            try:
                ready = self._ensure_managed_local_server_ready(
                    acquire_generation_lease=True,
                    lease_tickets=tickets,
                )
            except TypeError as exc:
                # Lightweight adapters may still expose the pre-lease helper
                # signature; production implementations use the ticket args.
                if "acquire_generation_lease" not in str(exc):
                    raise
                ready = self._ensure_managed_local_server_ready()
            if ready:
                return tickets[0] if tickets else None
            reason = str(self._last_local_server_ensure_error or "").strip()
            if not reason:
                reason = "llama-server の準備に失敗しました。"
                self._last_local_server_ensure_error = reason
            self._mark_runtime_unavailable(reason)
            raise ConnectionError(reason)
        config_error = self._managed_llama_cpp_launch_configuration_error()
        if config_error:
            self._last_local_server_ensure_error = config_error
            self._mark_runtime_unavailable(config_error)
            raise ConnectionError(config_error)
        return None

    def _catalog_capture_is_bound(self) -> bool:
        turn = get_turn_context()
        return bool(
            getattr(turn, "agent_execution_binding", None) is not None
            or isinstance(get_current_agent_tool_scope(), dict)
        )

    def _catalog_capture_role_and_controls(
        self,
    ) -> tuple[str, list[str] | None, bool]:
        turn = get_turn_context()
        role = str(getattr(turn, "agent_dialogue_role", "") or "").strip().casefold()
        if role == "root":
            return role, list(_CATALOG_CAPTURE_ROOT_CONTROL_NAMES), True
        if role == "worker":
            return role, list(_CATALOG_CAPTURE_WORKER_CONTROL_NAMES), True
        try:
            if self._is_bound_root_dialogue_turn() or self._is_bound_aoidots_root_turn():
                return "root", list(_CATALOG_CAPTURE_ROOT_CONTROL_NAMES), True
            if self._is_bound_worker_dialogue_turn():
                return "worker", list(_CATALOG_CAPTURE_WORKER_CONTROL_NAMES), True
        except Exception:
            pass
        return role or "unknown", None, False

    def _catalog_capture_hint_is_proven(self, payload: Mapping[str, Any]) -> bool:
        hint = str(getattr(self, "_current_tool_hint_context", "") or "")
        if not hint:
            return False
        trusted_context = getattr(self, "_current_prompt_dynamic_context", ()) or ()
        if not any(
            isinstance(item, (list, tuple))
            and len(item) == 2
            and item[0] == "Current tool hints"
            and item[1] == hint
            for item in trusted_context
        ):
            return False
        expected_digest = getattr(
            self, "_current_tool_hint_carrier_prefix_sha256", None
        )
        if not isinstance(expected_digest, str) or not expected_digest:
            return False
        actual_digest = _catalog_hint_carrier_prefix_digest(
            payload.get("messages", []) if isinstance(payload, Mapping) else [],
            hint,
        )
        return actual_digest == expected_digest

    def _emit_catalog_send_evidence(
        self,
        final_payload: Mapping[str, Any],
        *,
        sender_attempt_ordinal: int,
    ) -> None:
        """Emit only bounded catalog facts at the final SDK send boundary."""

        if not self._catalog_capture_is_bound():
            return
        try:
            native_present, native_valid, native_names = _catalog_names_from_native_payload(
                final_payload
            )
            role, expected_controls, expected_controls_known = (
                self._catalog_capture_role_and_controls()
            )
            run_id = self._current_agent_run_id_for_preprovider_proof()
            # The integer cap actually on the wire (no prompt/secret content).
            wire_max_tokens = None
            for cap_key in ("max_tokens", "max_completion_tokens"):
                cap = final_payload.get(cap_key) if isinstance(final_payload, Mapping) else None
                if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0:
                    wire_max_tokens = cap
                    break

            def emit_channel(
                channel: str,
                names: list[str] | None,
                *,
                proof_status: str,
            ) -> None:
                canonical_names = (
                    _canonical_catalog_names(names) if names is not None else None
                )
                if canonical_names is None:
                    display_names = None
                    names_digest = None
                    total_name_count = None
                    list_truncated = None
                    catalog_complete = None
                    read_chat_session_present = None
                    missing_controls = None
                else:
                    display_names = _display_catalog_names(canonical_names)
                    names_digest = _catalog_names_digest(canonical_names)
                    total_name_count = len(canonical_names)
                    list_truncated = total_name_count > _CATALOG_CAPTURE_MAX_NAMES
                    catalog_complete = not list_truncated
                    read_chat_session_present = "read_chat_session" in canonical_names
                    missing_controls = (
                        [
                            name
                            for name in expected_controls or ()
                            if name not in canonical_names
                        ]
                        if expected_controls_known
                        else None
                    )
                _emit_local_timing(
                    "generation",
                    "provider_catalog_send_attempt",
                    model=self.model_name,
                    provider="openai_compatible_local",
                    evidence_kind="sdk_send_attempt",
                    run_id=run_id,
                    turn_role=role,
                    request_ordinal=max(0, int(sender_attempt_ordinal)),
                    sender_attempt_ordinal=max(0, int(sender_attempt_ordinal)),
                    catalog_channel=channel,
                    proof_status=proof_status,
                    sorted_tool_names=display_names,
                    catalog_names_sha256=names_digest,
                    read_chat_session_present=read_chat_session_present,
                    total_name_count=total_name_count,
                    list_truncated=list_truncated,
                    catalog_complete=catalog_complete,
                    expected_control_names=(
                        expected_controls if expected_controls_known else None
                    ),
                    expected_controls_known=expected_controls_known,
                    missing_control_names=missing_controls,
                    actual_sender_entered=True,
                    sdk_call_boundary_reached=True,
                    wire_max_tokens=wire_max_tokens,
                )

            if native_present:
                emit_channel(
                    "native",
                    native_names if native_valid else None,
                    proof_status="proven" if native_valid else "unknown",
                )

            hint = str(getattr(self, "_current_tool_hint_context", "") or "")
            if hint:
                hint_names = _canonical_catalog_names(
                    getattr(self, "_current_tool_hint_catalog_names", ())
                )
                hint_proven = self._catalog_capture_hint_is_proven(final_payload)
                emit_channel(
                    "auxiliary_tool_hints"
                    if hint_proven
                    else "auxiliary_tool_hints_unproven",
                    hint_names if hint_proven else None,
                    proof_status="proven" if hint_proven else "unknown",
                )
            elif not native_present:
                emit_channel("none", [], proof_status="proven")
        except Exception:
            # Evidence is diagnostic-only and must never affect provider I/O.
            logger.debug("local catalog evidence emission failed", exc_info=True)

    @staticmethod
    def _reserve_catalog_sender_attempt() -> int:
        ordinal = _LOCAL_PROVIDER_SENDER_ATTEMPTS.get() + 1
        _LOCAL_PROVIDER_SENDER_ATTEMPTS.set(ordinal)
        return ordinal

    def _record_provider_response_diagnostic(
        self,
        response: Any,
        *,
        request_type: str,
    ) -> None:
        try:
            diagnostic = _safe_completion_response_diagnostic(
                response,
                request_type=request_type,
            )
        except Exception:
            sender_attempt_ordinal, _sender_attempt_failed = (
                _safe_local_sender_attempt_ordinal()
            )
            diagnostic = {
                "request_id": _LOCAL_TIMING_ID.get(),
                "request_type": (
                    request_type
                    if isinstance(request_type, str)
                    and request_type in _SAFE_LOCAL_REQUEST_TYPES
                    else "unknown"
                ),
                "sender_attempt_ordinal": sender_attempt_ordinal,
                "response_present": response is not None,
                "diagnostic_failed": True,
                "reasoning_close_retry_effective": None,
                "reasoning_close_retry_provenance": "unknown_live_process",
            }
        self._last_provider_response_diagnostic = diagnostic
        _emit_local_timing(
            "generation",
            "provider_response_shape",
            **{
                key: value
                for key, value in diagnostic.items()
                if key != "request_id"
            },
        )

    def _record_provider_response_text_lengths(
        self,
        message: Any,
        content: str,
    ) -> None:
        diagnostic = getattr(self, "_last_provider_response_diagnostic", None)
        if not isinstance(diagnostic, dict):
            return
        try:
            diagnostic["content_length"] = (
                len(content) if isinstance(content, str) else None
            )
            diagnostic["reasoning_length"] = _safe_reasoning_text_length(message)
        except Exception:
            diagnostic["content_length"] = None
            diagnostic["reasoning_length"] = None
            diagnostic["diagnostic_failed"] = True
        _emit_local_timing(
            "generation",
            "provider_response_shape",
            request_type=diagnostic.get("request_type", "unknown"),
            sender_attempt_ordinal=diagnostic.get("sender_attempt_ordinal", 0),
            content_length=diagnostic["content_length"],
            reasoning_length=diagnostic["reasoning_length"],
            reasoning_close_retry_effective=diagnostic[
                "reasoning_close_retry_effective"
            ],
            reasoning_close_retry_provenance=diagnostic[
                "reasoning_close_retry_provenance"
            ],
            diagnostic_failed=bool(diagnostic.get("diagnostic_failed", False)),
        )

    def _execute_completion_request(
        self,
        payload: Dict[str, Any],
        *,
        request_type: str = "chat",
        required_tool_schema: str | None = None,
    ) -> Any:
        """Run one OpenAI-compatible request through the egress transaction.

        The adapter is usually pointed at a loopback llama.cpp endpoint, but
        its URL is configurable.  A ``protect_sync`` preflight followed by a
        direct SDK call would therefore bypass ``review_policy=always`` when
        an operator configures a remote endpoint.  The sender below is the
        sole commit point and receives exactly the reviewed final mapping.
        """

        # Lightweight callers/tests may provide a no-op sync hook while
        # retaining the initialized gateway.  Keep the canonical execute
        # boundary in that compatibility shape rather than falling through to
        # a direct SDK call.
        if required_tool_schema is None:
            required_tool_schema = self._snapshot_required_tool_schema(payload)
        self._validate_required_tool_contract(payload, required_tool_schema)
        gateway = self._sync_privacy_gateway() or getattr(self, "_privacy_gateway", None)
        if gateway is None:
            raise PrivacyError("OpenAI-compatible privacy gateway is unavailable")
        descriptor = EgressDescriptor(
            action="model.generate",
            transport="openai-compatible.chat.completions",
            destination=self.base_url,
            provider="openai_compatible_local",
            tool="llm.openai_compatible_local",
            model=self.model_name,
        )

        from .provider_rate_governor import (
            GOVERNOR, budget_exhausted_after_429, governor_key, is_loopback_endpoint, should_retry_429,
        )
        from ..services.free_team_service import openai_credential_identity

        governor_endpoint = str(self.base_url or "")
        governed = not is_loopback_endpoint(governor_endpoint)
        governor_id = governor_key(
            "openai_compatible_local", governor_endpoint, self.model_name,
            openai_credential_identity(self.client).get("api_key_sha256"),
        )

        def checkpoint() -> float | None:
            active = current_generation_budget.get()
            return active.checkpoint() if active is not None else None

        lease_box: dict[str, Any] = {}

        def sender(final_payload: Any) -> Any:
            # The slot follows the actual request; a stream hands it to the
            # stream object, which releases it only once cleanup is verified.
            lease = GOVERNOR.acquire(governor_id, checkpoint) if governed else None
            lease_box["lease"] = lease
            lease_box["handed"] = False
            try:
                result = raw_sender(final_payload)
            except BaseException:
                # An InterruptibleStream (or its reaper) owns a handed-over
                # lease; only an unowned or plain-wrapper lease is released here.
                if lease is not None and lease_box["handed"] != "interruptible":
                    lease.release()
                raise
            if lease is not None and not lease_box["handed"]:
                lease.release()
            return result

        def raw_sender(final_payload: Any) -> Any:
            if not isinstance(final_payload, dict):
                raise PrivacyError("OpenAI-compatible outbound payload is malformed")
            self._validate_required_tool_contract(final_payload, required_tool_schema)
            self._last_privacy_payload = dict(final_payload)
            budget = current_generation_budget.get()
            client = self.client
            if budget is not None:
                budget.record_request(final_payload)
                if budget.interruptible_transport:
                    from .bounded_completion import execute_interruptible_completion, execute_interruptible_stream

                    sender_attempt_ordinal = self._reserve_catalog_sender_attempt()
                    execute = (
                        execute_interruptible_stream
                        if final_payload.get("stream")
                        else execute_interruptible_completion
                    )
                    extra_args: dict[str, Any] = {}
                    if final_payload.get("stream"):
                        extra_args["lease"] = lease_box.get("lease")
                        lease_box["handed"] = "interruptible"
                    response = execute(
                        client,
                        final_payload,
                        budget,
                        on_send=lambda: self._emit_catalog_send_evidence(
                            final_payload,
                            sender_attempt_ordinal=sender_attempt_ordinal,
                        ),
                        **extra_args,
                    )
                    try:
                        self._last_provider_response_shape = (
                            _safe_completion_response_shape(response)
                        )
                    except Exception:
                        self._last_provider_response_shape = None
                    self._record_provider_response_diagnostic(
                        response,
                        request_type=request_type,
                    )
                    return response
                remaining = budget.checkpoint()
                # SDK retries would outlive our single deadline. Compatibility
                # retries remain explicit and pass this same checkpoint.
                options = {"max_retries": 0}
                if remaining is not None:
                    options["timeout"] = remaining
                client = client.with_options(**options)
            sender_attempt_ordinal = self._reserve_catalog_sender_attempt()
            self._emit_catalog_send_evidence(
                final_payload,
                sender_attempt_ordinal=sender_attempt_ordinal,
            )
            if governed and budget is None:
                # SDK retries would multiply the governor's 3-attempt bound and
                # skip the privacy/budget boundary; retries stay explicit.
                client = client.with_options(max_retries=0)
            response = client.chat.completions.create(**final_payload)
            if final_payload.get("stream") and lease_box.get("lease") is not None:
                # The slot must outlive the handshake: the wrapper releases it
                # when the real stream ends, errors or is closed.
                from .provider_rate_governor import LeasedSyncStream

                response = LeasedSyncStream(response, lease_box["lease"])
                lease_box["handed"] = True
            try:
                self._last_provider_response_shape = _safe_completion_response_shape(
                    response
                )
            except Exception:
                self._last_provider_response_shape = None
            self._record_provider_response_diagnostic(
                response,
                request_type=request_type,
            )
            if budget is not None:
                budget.checkpoint()
                model = getattr(response, "model", None)
                if isinstance(model, str):
                    budget.response_model = model
            return response

        self._last_privacy_payload = dict(payload)
        physical = 0
        prior_429: BaseException | None = None
        while True:
            physical += 1
            try:
                if governed:
                    GOVERNOR.wait_cooldown(governor_id, checkpoint)
                # Each physical attempt re-enters the privacy gateway and the
                # request budget once; only a handshake-time temporary 429 repeats.
                return gateway.execute_sync(
                    payload,
                    provider="openai_compatible_local",
                    descriptor=descriptor,
                    sender=sender,
                    base_url=self.base_url,
                    source_kind=request_type,
                    model=self.model_name,
                )
            except Exception as error:
                if budget_exhausted_after_429(error, prior_429):
                    raise prior_429 from None
                if not (governed and should_retry_429(error, physical, governor_id, checkpoint)):
                    raise
                prior_429 = error

    def _execute_retry_request(
        self,
        retry_kwargs: Dict[str, Any],
        *,
        reason: str,
        required_tool_schema: str | None = None,
    ) -> Any:
        retry_started_at = time.monotonic()
        try:
            response = self._execute_completion_request(
                retry_kwargs,
                request_type="retry",
                required_tool_schema=required_tool_schema,
            )
        except Exception:
            self._mark_request_usage_unknown()
            raise
        self._capture_context_request(
            getattr(self, "_last_privacy_payload", retry_kwargs),
            reason=reason,
        )
        self._capture_and_persist_usage(
            response,
            request_type="retry",
            latency_ms=max(0, int((time.monotonic() - retry_started_at) * 1000)),
        )
        return response

    def _tool_results_compacted_for_retry(
        self,
        api_kwargs: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Compact tool-result messages for an overflow retry.

        Retrieval tools return JSON contracts whose ``read_args`` and citation
        objects must survive the retry byte-for-byte.  The old generic string
        clipper could leave invalid JSON and make a later ``media_read`` call
        impossible, so only result count/snippets or canonical text are
        reduced for those tools.
        """
        messages = list(api_kwargs.get("messages") or [])
        tool_count = sum(
            1 for message in messages
            if isinstance(message, dict) and message.get("role") == "tool"
        )
        if not tool_count:
            return None
        budget = self._current_context_budget or self._context_budget()
        limit = max(
            600,
            min(
                TOOL_RESULT_OVERFLOW_RETRY_CHARS,
                budget.message_budget_chars // (3 * tool_count),
            ),
        )
        changed = False
        compacted: list[Any] = []
        for index, message in enumerate(messages):
            content = message.get("content") if isinstance(message, dict) else None
            if (
                isinstance(message, dict)
                and message.get("role") == "tool"
                and isinstance(content, str)
                and len(content) > limit
            ):
                tool_name = _tool_name_from_message(messages, index)
                compacted_content = _compact_tool_result_content(
                    tool_name,
                    content,
                    limit,
                )
                if compacted_content != content:
                    message = {**message, "content": compacted_content}
                    changed = True
            compacted.append(message)
        if not changed:
            return None
        return {**api_kwargs, "messages": compacted}

    def _tool_loop_clip_scope(self, messages: list[Any]) -> str:
        """Identify one tool loop for the remembered clip levels.

        The scope is the session/user plus the loop's own opening messages
        (system prompt and first user request).  It only keys integer clip
        levels; no transcript text is stored or shared.
        """

        opening = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            opening.append(
                [message.get("role"), _compact_json(message.get("content"))]
            )
            if message.get("role") == "user":
                break
        payload = _compact_json(
            [
                str(getattr(self, "_current_session_id", "") or ""),
                str(getattr(self, "session_user_id", "") or ""),
                str(getattr(self, "model_name", "") or ""),
                opening,
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

    def _fit_tool_loop_messages_to_budget(self, api_kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """Shrink a tool-loop request until its estimate fits the input tokens.

        The message budget does not include native tool schemas, so a tool loop
        can overflow even though each message was clipped on its own.  The
        shaping is deterministic and only touches the outgoing copy.  Older
        results give way first (clipped, then replaced by a stub that names the
        call), then long older call arguments are shortened, and only as a last
        resort are the newest results clipped.  Clipping every result evenly
        made the model re-read the same range because the slice it had just
        requested came back cut to a few lines.  Call/result pairing and the
        system and user messages are never dropped.

        A delegated task drives this client through ``_tool_loop_completion``
        outside ``chat()``, where no per-turn budget may be stored, so the live
        model budget is resolved here instead of skipping the fit.
        """
        messages = api_kwargs.get("messages")
        if not _has_tool_result_messages(messages):
            return api_kwargs
        budget = self._current_context_budget or self._context_budget()
        schema_tokens = estimate_prompt_tokens(_compact_json(api_kwargs.get("tools") or []))
        input_limit = self._tool_loop_input_limit(api_kwargs, budget)

        def fits(candidate: Dict[str, Any]) -> bool:
            return (
                estimate_prompt_tokens(_compact_json(candidate.get("messages") or []))
                + schema_tokens
                <= input_limit
            )

        tool_indexes = [
            index
            for index, message in enumerate(messages)
            if isinstance(message, dict)
            and message.get("role") == "tool"
            and isinstance(message.get("content"), str)
        ]
        protected = set(tool_indexes[-TOOL_LOOP_KEEP_RECENT_RESULTS:])
        older = {index for index in tool_indexes if index not in protected}

        def clipped(base: Dict[str, Any], limit: int, indexes: set[int]) -> Dict[str, Any]:
            base_messages = base["messages"]
            return {
                **base,
                "messages": [
                    (
                        {
                            **message,
                            "content": _compact_tool_result_content(
                                _tool_name_from_message(base_messages, index),
                                message["content"],
                                limit,
                            ),
                        }
                        if index in indexes
                        else message
                    )
                    for index, message in enumerate(base_messages)
                ],
            }

        limits = (8000, 4800, TOOL_RESULT_OVERFLOW_RETRY_CHARS, 1200)

        # Older results only ever get shorter (a remembered level per result)
        # and a new pass reclaims down to a lower target, so consecutive rounds
        # send the same prefix and the engine's KV/prefix cache stays valid
        # between passes.  Re-clipping every older result from the current
        # total on every round rewrote the whole prefix each time.
        stub_level = len(limits) + 1
        scope = self._tool_loop_clip_scope(messages)
        state = self.__dict__.setdefault("_tool_loop_clip_levels", OrderedDict())
        keys = {
            index: (scope, str(messages[index].get("tool_call_id") or ""),
                    hashlib.sha256(messages[index]["content"].encode("utf-8")).hexdigest()[:24])
            for index in older
        }
        levels = {index: state.get(keys[index], 0) for index in older}
        if not any(levels.values()) and fits(api_kwargs):
            return api_kwargs
        projected: dict[tuple[int, int], str] = {}

        def projection(index: int, level: int) -> str:
            if (index, level) not in projected:
                projected[(index, level)] = (
                    _omitted_tool_result_stub(messages, index)
                    if level >= stub_level
                    else _compact_tool_result_content(
                        _tool_name_from_message(messages, index),
                        messages[index]["content"],
                        limits[level - 1],
                    )
                )
            return projected[(index, level)]

        def project() -> Dict[str, Any]:
            return {
                **api_kwargs,
                "messages": [
                    (
                        {**message, "content": projection(index, levels[index])}
                        if levels.get(index)
                        else message
                    )
                    for index, message in enumerate(messages)
                ],
            }

        candidate = project()
        if fits(candidate):
            return candidate
        # Hard budget exceeded: reclaim to a target below it (minimum reclaim)
        # instead of stopping at the line, oldest results first and one level
        # deeper at a time, so only the oldest results are rewritten.
        target_tokens = int(
            min(
                input_limit * TOOL_LOOP_RECLAIM_TARGET,
                budget.context_window_tokens * TOOL_LOOP_RECLAIM_WINDOW_SHARE,
            )
        )
        tokens = estimate_prompt_tokens(_compact_json(candidate["messages"])) + schema_tokens
        for level in range(1, stub_level + 1):
            for index in sorted(older):
                if tokens <= target_tokens:
                    break
                if levels[index] >= level:
                    continue
                before = projection(index, levels[index]) if levels[index] else messages[index]["content"]
                after = projection(index, level)
                if len(after) >= len(before):
                    continue
                tokens -= estimate_prompt_tokens(before) - estimate_prompt_tokens(after)
                levels[index] = level
            if tokens <= target_tokens:
                break
        for index, level in levels.items():
            if level:
                state.pop(keys[index], None)
                state[keys[index]] = level
        while len(state) > TOOL_LOOP_CLIP_STATE_LIMIT:
            state.popitem(last=False)
        candidate = project()
        if not fits(candidate):
            candidate = self._omit_oldest_tool_loop_history(candidate, fits, protected)
        if not fits(candidate):
            base = candidate
            # Folding groups shifts positions: pick the newest results again.
            newest = [
                index
                for index, message in enumerate(base["messages"])
                if isinstance(message, dict)
                and message.get("role") == "tool"
                and isinstance(message.get("content"), str)
            ][-TOOL_LOOP_KEEP_RECENT_RESULTS:]
            for limit in limits:
                candidate = clipped(base, limit, set(newest))
                if fits(candidate):
                    break
        logger.warning(
            "[OpenAICompatibleLocalClient] Clipped tool results to fit the local input budget (%s tokens)",
            input_limit,
        )
        return candidate

    def _tool_loop_input_limit(
        self, api_kwargs: Dict[str, Any], budget: ContextBudget
    ) -> int:
        """Input tokens a tool-loop request may use.

        The planning budget reserves only ``response_tokens`` (1024).  With the
        explicit ``tool_generation_max_tokens`` opt-in the wire output is up to
        that value, so messages + schemas must leave that much room (plus the
        safety margin) before sending; otherwise the wire cap is cut later and
        a long tool-code generation ends with ``finish_reason=length``.  Never
        raises the planning limit, never applies without the opt-in, and a
        lower explicit cap (or scoped override) keeps the planning limit.
        """

        if self._scoped_response_tokens_override() is not None:
            return budget.input_tokens
        getter = getattr(getattr(self, "config", None), "get", None)
        optin = (
            getter(
                "openai_compatible_local.context_budget.tool_generation_max_tokens",
                None,
            )
            if callable(getter)
            else None
        )
        try:
            optin = int(optin) if optin not in (None, "", False) else 0
        except (TypeError, ValueError):
            optin = 0
        max_tokens = api_kwargs.get("max_tokens")
        if optin <= 0 or not isinstance(max_tokens, int) or isinstance(max_tokens, bool):
            return budget.input_tokens
        output = min(max_tokens, optin, TOOL_GENERATION_ALLOWANCE_TOKENS)
        safety = max(384, int(budget.context_window_tokens * 0.06))
        return min(budget.input_tokens, budget.context_window_tokens - safety - output)

    @staticmethod
    def _omit_oldest_tool_loop_history(
        api_kwargs: Dict[str, Any],
        fits: Callable[[Dict[str, Any]], bool],
        protected: set[int],
    ) -> Dict[str, Any]:
        """Stub the oldest tool results, then shorten their call arguments.

        Every assistant ``tool_calls`` entry keeps its matching ``tool`` message
        (the wire format requires the pairing); only the bodies shrink.  The
        stub names the call so the model knows what it already read and does
        not repeat it.  Results in ``protected`` stay whole.
        """
        messages = [
            dict(message) if isinstance(message, dict) else message
            for message in api_kwargs.get("messages") or []
        ]
        candidate = {**api_kwargs, "messages": messages}
        for index, message in enumerate(messages):
            if (
                index in protected
                or not isinstance(message, dict)
                or message.get("role") != "tool"
                or not isinstance(message.get("content"), str)
            ):
                continue
            stub = _omitted_tool_result_stub(messages, index)
            if len(message["content"]) > len(stub):
                message["content"] = stub
                if fits(candidate):
                    return candidate
        last_assistant = max(
            (
                index
                for index, message in enumerate(messages)
                if isinstance(message, dict) and message.get("role") == "assistant"
            ),
            default=-1,
        )
        for string_limit in TOOL_LOOP_ARGUMENT_STRING_LIMITS:
            for index, message in enumerate(messages):
                if index == last_assistant or not isinstance(message, dict):
                    continue
                calls = message.get("tool_calls")
                if message.get("role") != "assistant" or not isinstance(calls, list):
                    continue
                shortened = []
                for call in calls:
                    function = call.get("function") if isinstance(call, dict) else None
                    arguments = function.get("arguments") if isinstance(function, dict) else None
                    short = (
                        _shorten_tool_call_arguments(arguments, string_limit)
                        if isinstance(arguments, str)
                        else arguments
                    )
                    if short is arguments:
                        shortened.append(call)
                    else:
                        shortened.append({**call, "function": {**function, "arguments": short}})
                message["tool_calls"] = shortened
            if fits(candidate):
                break
        if fits(candidate):
            return candidate
        return _collapse_oldest_tool_loop_groups(candidate, fits, protected_calls=TOOL_LOOP_KEEP_RECENT_RESULTS)

    def _create_completion_with_fallback(
        self,
        api_kwargs: Dict[str, Any],
        *,
        request_type: str = "chat",
        allow_connection_retry: bool = True,
        required_tool_schema: str | None = None,
    ) -> Any:
        """Run one logical request with a turn-local sender-attempt counter."""

        depth = _LOCAL_PROVIDER_REQUEST_DEPTH.get()
        depth_token = _LOCAL_PROVIDER_REQUEST_DEPTH.set(depth + 1)
        sender_token = None
        if depth == 0 and not _LOCAL_PROVIDER_TURN_ACTIVE.get():
            sender_token = _LOCAL_PROVIDER_SENDER_ATTEMPTS.set(0)
        validate_terminal = depth == 0 and request_type in {
            "chat", "tool", "retry", "continuation", "review",
        }
        observe_terminal = validate_terminal and _LOCAL_PROVIDER_TERMINAL_TRACKING.get() and request_type != "review"
        budget = current_generation_budget.get()
        if budget is not None and budget.interruptible_transport and _has_tool_result_messages(api_kwargs.get("messages")):
            # A connection loss after actual tool effects is an uncertain
            # outcome. Let task settlement preserve the receipts; do not
            # initiate another provider/tool turn through server recovery.
            allow_connection_retry = False
        try:
            response = self._create_completion_with_fallback_impl(
                api_kwargs,
                request_type=request_type,
                allow_connection_retry=allow_connection_retry,
                required_tool_schema=required_tool_schema,
            )
            if validate_terminal:
                self._observe_provider_terminal(
                    response, record=observe_terminal,
                    request_type="review" if not _LOCAL_PROVIDER_TERMINAL_TRACKING.get() or request_type == "review" else request_type,
                )
            return response
        except Exception as exc:
            if validate_terminal and (
                isinstance(exc, (httpx.TransportError, APIError))
                or classify_generation_error(exc).kind in {GenerationErrorKind.CONNECTION, GenerationErrorKind.TIMEOUT}
            ):
                evidence = interrupted_terminal("chat.completions")
                if observe_terminal:
                    self._last_provider_terminal_evidence = evidence
                else:
                    failure = ModelOutputIncompleteError(
                        "provider_interrupted", transport="chat.completions", request_type="review",
                    )
                    failure.provider_terminal = evidence
                    raise failure from exc
            raise
        finally:
            _LOCAL_PROVIDER_REQUEST_DEPTH.reset(depth_token)
            if sender_token is not None:
                _LOCAL_PROVIDER_SENDER_ATTEMPTS.reset(sender_token)

    def _observe_provider_terminal(self, response: Any, *, record: bool = True, request_type: str = "") -> None:
        evidence = normalize_chat_terminal(response)
        choices = getattr(response, "choices", None) or []
        message = getattr(choices[0], "message", None) if choices else None
        if evidence["state"] == "continuation":
            if not getattr(message, "tool_calls", None):
                evidence = protocol_error_terminal(evidence, "missing_tool_calls")
            elif request_type == "review":
                evidence = protocol_error_terminal(evidence, "unconsumed_continuation")
        elif request_type == "review" and getattr(message, "tool_calls", None):
            # A tool-free reviewer must not reach tool execution whatever
            # finish_reason (stop/absent included) accompanies the calls.
            evidence = protocol_error_terminal(evidence, "review_tool_calls")
        if record:
            self._last_provider_terminal_evidence = evidence
        if evidence["state"] in FAILED_TERMINAL_STATES:
            # Check before interpreting tool calls or retrying empty reasoning.
            # Usage has already been captured; this accepted request must not
            # be retried as a compatibility error or replay a write.
            failure = ModelOutputIncompleteError(
                evidence.get("protocol_error") or evidence["finish_reason"],
                transport="chat.completions", request_type=request_type,
            )
            failure.provider_terminal = evidence
            raise failure

    def _create_completion_with_fallback_impl(
        self,
        api_kwargs: Dict[str, Any],
        *,
        request_type: str = "chat",
        allow_connection_retry: bool = True,
        required_tool_schema: str | None = None,
    ) -> Any:
        # Freeze once per logical request, before runtime preparation or egress.
        # Every explicit retry carries this immutable value, even when a gateway
        # mutates the caller's payload and raises before invoking its sender.
        if required_tool_schema is None:
            required_tool_schema = self._snapshot_required_tool_schema(api_kwargs)
        self._validate_required_tool_contract(api_kwargs, required_tool_schema)
        required_tool_request = required_tool_schema is not None
        budget = current_generation_budget.get()
        if budget is not None:
            budget.checkpoint()
        self._last_provider_response_shape = None
        self._last_provider_response_diagnostic = None
        failure_stage = "provider_prepare"
        try:
            lease_ticket = self._prepare_managed_local_server_for_request()
        except Exception as exc:
            wrapped = self._wrap_preprovider_readiness_failure(
                exc,
                request_type=request_type,
            )
            if wrapped is not exc:
                raise wrapped from exc
            if budget is not None:
                budget.checkpoint()
            raise
        try:
            if budget is not None:
                budget.checkpoint()
            started_at = time.monotonic()
            _emit_local_timing(
                "generation",
                "start",
                model=self.model_name,
                request_type=request_type,
                **_local_timing_input_stats(api_kwargs),
            )
            try:
                failure_stage = "provider_send"
                response = self._execute_completion_request(
                    api_kwargs,
                    request_type=request_type,
                    required_tool_schema=required_tool_schema,
                )
                failure_stage = "context_capture"
                self._capture_context_request(
                    getattr(self, "_last_privacy_payload", api_kwargs),
                    reason="chat.completions",
                )
                failure_stage = "usage_capture"
                self._capture_and_persist_usage(
                    response,
                    request_type=request_type,
                    latency_ms=max(0, int((time.monotonic() - started_at) * 1000)),
                )
                metrics = self._last_generation_metrics or {}
                _emit_local_timing(
                    "generation",
                    "end",
                    started_at=started_at,
                    model=self.model_name,
                    request_type=request_type,
                    input_tokens=metrics.get("prompt_tokens"),
                    output_tokens=metrics.get("output_tokens"),
                    generation_ms=metrics.get("generation_ms"),
                    prompt_ms=metrics.get("prompt_ms"),
                    **_local_timing_input_stats(api_kwargs),
                )
                self._clear_runtime_unavailability()
                return response
            except Exception as exc:
                # The provider may have received the request without returning
                # a receipt. Keep this request unknown across any successful
                # compatibility/recovery retry; never infer zero usage.
                self._mark_request_usage_unknown()
                _emit_local_timing(
                    "generation",
                    "error",
                    started_at=started_at,
                    model=self.model_name,
                    request_type=request_type,
                    error_code=type(exc).__name__,
                    error_stage=failure_stage,
                    response_shape_observed=bool(
                        self._last_provider_response_shape
                    ),
                    **_local_timing_input_stats(api_kwargs),
                )
                if budget is not None:
                    budget.checkpoint()
                if isinstance(exc, LocalToolSelectionError):
                    raise
                self._validate_required_tool_contract(api_kwargs, required_tool_schema)
                if _is_context_overflow_error(exc) and "tools" in api_kwargs:
                    base_kwargs = api_kwargs
                    compacted_kwargs = self._tool_results_compacted_for_retry(api_kwargs)
                    if compacted_kwargs is not None:
                        # Keep the tools: clipped tool results usually fit, and
                        # the loop can still continue instead of guessing.
                        logger.warning(
                            "[OpenAICompatibleLocalClient] Context overflow in tool loop; retrying with clipped tool results: %s",
                            exc,
                        )
                        try:
                            return self._execute_retry_request(
                                compacted_kwargs,
                                reason="context_overflow_tool_result_retry",
                                required_tool_schema=required_tool_schema,
                            )
                        except Exception as compact_exc:
                            if not _is_context_overflow_error(compact_exc):
                                raise
                        base_kwargs = compacted_kwargs
                    if required_tool_request:
                        raise LocalToolSelectionError(
                            "必須ツール指定を維持したリクエストがコンテキスト上限を超えました。"
                            "必須条件を解除せず、生成を中止しました。"
                        ) from exc
                    retry_kwargs = dict(base_kwargs)
                    retry_kwargs.pop("tools", None)
                    retry_kwargs.pop("tool_choice", None)
                    self._enforce_strata_final_answer_contract(retry_kwargs)
                    if _has_tool_result_messages(retry_kwargs.get("messages")):
                        # Without native tools a local model tends to print the
                        # call it wanted; ask for the final answer explicitly.
                        retry_kwargs["messages"] = [
                            *retry_kwargs["messages"],
                            {"role": "user", "content": TOOL_LOOP_FINAL_ANSWER_INSTRUCTION},
                        ]
                    logger.warning(
                        "[OpenAICompatibleLocalClient] Context overflow with native tools; retrying without tools: %s",
                        exc,
                    )
                    return self._execute_retry_request(
                        retry_kwargs,
                        reason="context_overflow_retry",
                        required_tool_schema=required_tool_schema,
                    )
                if _is_local_model_loading_error(exc) or _is_context_overflow_error(exc):
                    raise
                if allow_connection_retry and _is_connection_error(exc):
                    recovery_retry_handled = False
                    if self._can_attempt_managed_llama_cpp_recovery():
                        recovery_retry_handled = True
                        # Connection recovery is still one request, not a new
                        # long-lived route selection.  Use a generation-only
                        # ticket so the shared-runtime bridge does not create
                        # its persistent ``_routes`` consumer retention while
                        # an evicted/retired response-model client is finishing
                        # a title or tool round.  Keep this local ticket alive
                        # through the retry and release only it; the outer
                        # request ticket is owned by this method's finally.
                        recovery_lease_tickets: list[Any] = []
                        try:
                            try:
                                recovered = self._ensure_managed_local_server_ready(
                                    acquire_generation_lease=True,
                                    lease_tickets=recovery_lease_tickets,
                                )
                            except TypeError as recovery_exc:
                                # Lightweight legacy test doubles may still
                                # expose the pre-lease helper signature.  The
                                # production helper above always accepts the
                                # generation-only keywords; retain the old
                                # seam only for those doubles.
                                if (
                                    "acquire_generation_lease"
                                    not in str(recovery_exc)
                                    and "lease_tickets" not in str(recovery_exc)
                                ):
                                    raise
                                recovered = self._ensure_managed_local_server_ready()
                            if recovered:
                                logger.warning(
                                    "[OpenAICompatibleLocalClient] Connection error; retrying after local server recovery: %s",
                                    exc,
                                )
                                return self._create_completion_with_fallback(
                                    api_kwargs,
                                    request_type=request_type,
                                    allow_connection_retry=False,
                                    required_tool_schema=required_tool_schema,
                                )
                        finally:
                            for recovery_ticket in recovery_lease_tickets:
                                if not bool(
                                    getattr(recovery_ticket, "_released", False)
                                ):
                                    recovery_ticket.release()
                    elif self._managed_llama_cpp_launch_configuration_error():
                        config_error = self._managed_llama_cpp_launch_configuration_error()
                        self._last_local_server_ensure_error = str(config_error or "")
                        self._mark_runtime_unavailable(self._last_local_server_ensure_error)
                        recovered = False
                    elif self._is_manual_managed_llama_cpp_runtime():
                        message = (
                            "手動接続先の llama-server に接続できません。"
                            " endpoint が起動しているか確認してください。"
                        )
                        self._last_local_server_ensure_error = message
                        self._mark_runtime_unavailable(message)
                        recovered = False
                    else:
                        recovered = self._wait_for_local_server_health(timeout_seconds=5.0)
                    if recovered and not recovery_retry_handled:
                        logger.warning(
                            "[OpenAICompatibleLocalClient] Connection error; retrying after local server recovery: %s",
                            exc,
                        )
                        return self._create_completion_with_fallback(
                            api_kwargs,
                            request_type=request_type,
                            allow_connection_retry=False,
                            required_tool_schema=required_tool_schema,
                        )
                if _is_connection_error(exc):
                    if not self.is_runtime_known_unavailable():
                        reason = str(self._last_local_server_ensure_error or exc)
                        self._mark_runtime_unavailable(reason)
                    raise
                if (
                    failure_stage != "provider_send"
                    or self._last_provider_response_shape is not None
                    or not _is_unsupported_optional_parameter_error(exc, api_kwargs)
                ):
                    logger.warning(
                        "[OpenAICompatibleLocalClient] local completion failed; "
                        "compatibility retry skipped stage=%s error_type=%s "
                        "response_shape=%s traceback=%s",
                        failure_stage,
                        type(exc).__name__,
                        self._last_provider_response_shape,
                        _safe_traceback_frames(exc),
                    )
                    raise
                retry_kwargs, removed = self._compatibility_retry_kwargs(api_kwargs)
                if not removed:
                    if required_tool_request:
                        raise LocalToolSelectionError(
                            "必須ツール指定を維持したリクエストが接続先に拒否されました。"
                            "接続先のtool対応とjinja設定を確認してください。"
                            "必須条件を解除した再送は行いません。"
                        ) from exc
                    raise
                logger.warning(
                    "[OpenAICompatibleLocalClient] Compatibility retry without %s "
                    "after explicit unsupported-parameter response error_type=%s",
                    ", ".join(removed),
                    type(exc).__name__,
                )
                retry_started_at = time.monotonic()
                try:
                    response = self._execute_completion_request(
                        retry_kwargs,
                        request_type="retry",
                        required_tool_schema=required_tool_schema,
                    )
                except Exception as retry_exc:
                    self._mark_request_usage_unknown()
                    if required_tool_request and not (
                        isinstance(retry_exc, LocalToolSelectionError)
                        or _is_connection_error(retry_exc)
                        or _is_local_model_loading_error(retry_exc)
                        or _is_context_overflow_error(retry_exc)
                    ):
                        raise LocalToolSelectionError(
                            "必須ツール指定を維持した再送も接続先に拒否されました。"
                            "接続先のtool対応とjinja設定を確認してください。"
                        ) from retry_exc
                    raise
                self._capture_context_request(
                    getattr(self, "_last_privacy_payload", retry_kwargs),
                    reason="compatibility_retry",
                )
                self._capture_and_persist_usage(
                    response,
                    request_type="retry",
                    latency_ms=max(0, int((time.monotonic() - retry_started_at) * 1000)),
                )
                return response
        finally:
            if lease_ticket is not None:
                lease_ticket.release()

    def _message_content(self, message: Any) -> str:
        content = _message_field(message, "content")
        if not isinstance(content, str):
            return ""
        return _strip_leading_think_markup(content)

    def _stream_reasoning_markup_supported(self) -> bool:
        """Return whether leading ``<think>`` content is provider markup.

        Generic external OpenAI-compatible endpoints keep their historical
        literal-text behavior.  Managed reasoning profiles and an explicitly
        selected Strata thinking mode have a model-owned reasoning contract,
        so their leading tag can be separated from the final answer.
        """

        if getattr(self, "_profile_thinking_mode", None) == "always":
            return True
        if getattr(self, "_profile_reasoning_supported", None) is True:
            return True
        metadata_getter = getattr(self, "_reasoning_effort_metadata", None)
        if callable(metadata_getter):
            try:
                if metadata_getter():
                    return True
            except Exception:
                # A lightweight/test client may not have complete profile
                # state.  Its raw stream must retain ordinary text.
                pass
        if getattr(self, "_managed_local_runtime", None) == "strata":
            selected = getattr(self, "_strata_thinking_mode_selected", None)
            if callable(selected):
                try:
                    return bool(selected())
                except Exception:
                    return False
        return False

    def _record_model_transcript(
        self,
        messages: List[Dict[str, Any]],
        response_text: str,
    ) -> None:
        if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            # Keep the provider's ordinary transcript untouched.  TerminalMode
            # snapshots/restores this state around Help; this guard also covers
            # direct provider callers that bypass that outer lifecycle.
            self._last_model_transcript = []
            return
        source_messages = self._last_tool_loop_messages or messages
        self._last_model_transcript = canonical_model_transcript_for_history(
            [
                dict(message)
                for message in source_messages
                if message.get("role") in {"user", "assistant", "tool"}
            ],
            current_user_input=get_current_user_input(),
        )
        if response_text:
            self._last_model_transcript.append(
                {"role": "assistant", "content": response_text}
            )
        if hasattr(self.history_manager, "set_model_messages"):
            self.history_manager.set_model_messages(self._last_model_transcript)

    def _chat_template_thinking_value(self, api_kwargs: Dict[str, Any]) -> Any:
        extra_body = api_kwargs.get("extra_body")
        if not isinstance(extra_body, dict):
            return None
        template_kwargs = extra_body.get("chat_template_kwargs")
        if not isinstance(template_kwargs, dict):
            return None
        return template_kwargs.get("enable_thinking")

    def _should_retry_without_thinking(
        self, message: Any, api_kwargs: Dict[str, Any]
    ) -> bool:
        if getattr(self, "_profile_thinking_mode", None) == "always":
            # A reasoning-only response is expected for this model when the
            # output reserve is exhausted.  Retrying with a generic
            # ``enable_thinking=false`` flag is both unsupported by the
            # official template and contrary to its always-thinking contract.
            return False
        # Strata's selected Thinking mode is a user-visible contract.  A
        # reasoning-only response can exhaust its output reserve, but retrying
        # with ``enable_thinking=false`` would silently change that contract.
        # The caller's bounded empty-response path keeps the same wire mode or
        # surfaces a controlled empty result instead.
        if self._strata_thinking_mode_selected():
            return False
        # Managed Qwen3.8 has no disable-thinking switch.  A reasoning-only
        # response may still be retried, but the same profile effort must be
        # preserved instead of adding enable_thinking=false.
        if self._reasoning_effort_metadata():
            return _message_has_reasoning_output(message)
        if self._chat_template_thinking_value(api_kwargs) is False:
            return False
        if _message_has_reasoning_output(message):
            return True
        return False

    def _create_completion_retrying_empty_reasoning(
        self,
        api_kwargs: Dict[str, Any],
        *,
        reason: str,
        request_type: str = "chat",
    ) -> Any:
        self._bound_wire_max_tokens_to_window(api_kwargs)
        required_tool_schema = self._snapshot_required_tool_schema(api_kwargs)
        contract_kwargs = (
            {"required_tool_schema": required_tool_schema}
            if required_tool_schema is not None
            else {}
        )
        response = self._create_completion_with_fallback(
            api_kwargs,
            request_type=request_type,
            **contract_kwargs,
        )
        budget = current_generation_budget.get()
        if budget is not None:
            budget.checkpoint()
        choice = response.choices[0]
        message = choice.message
        if getattr(message, "tool_calls", None) or self._message_content(message):
            return response
        if self._is_help_isolated_turn():
            return response
        if not self._should_retry_without_thinking(message, api_kwargs):
            if (
                self._strata_thinking_mode_selected()
                and _message_has_reasoning_output(message)
            ):
                logger.warning(
                    "[OpenAICompatibleLocalClient] Empty content with Strata "
                    "Thinking reasoning output; retaining selected thinking "
                    "mode and using bounded empty-response handling (%s)",
                    reason,
                )
            return response
        logger.warning(
            "[OpenAICompatibleLocalClient] Empty content with reasoning output; "
            "retrying with thinking disabled (%s)",
            reason,
        )
        if self._reasoning_effort_metadata():
            retry_kwargs = dict(api_kwargs)
            retry_extra = retry_kwargs.get("extra_body")
            retry_extra = retry_extra if isinstance(retry_extra, dict) else {}
            retry_extra = _deep_merge_dict(retry_extra, self._mode_extra_body())
            retry_template = retry_extra.get("chat_template_kwargs")
            if isinstance(retry_template, dict):
                retry_template.pop("enable_thinking", None)
            retry_extra.pop("reasoning_effort", None)
            retry_extra.pop("enable_thinking", None)
            retry_kwargs["extra_body"] = retry_extra
        else:
            retry_kwargs = self._set_chat_template_thinking(api_kwargs, False)
        api_kwargs.clear()
        api_kwargs.update(retry_kwargs)
        return self._create_completion_with_fallback(
            api_kwargs,
            request_type="retry",
            **contract_kwargs,
        )

    def _empty_response_fallback(self) -> str:
        self._last_generation_failure = empty_response_failure()
        set_current_compatible_generation_failure(self._last_generation_failure)
        return LOCAL_MODEL_EMPTY_RESPONSE

    def _tool_loop_completion(self, api_kwargs: Dict[str, Any]) -> Any:
        api_kwargs = dict(self._fit_tool_loop_messages_to_budget(api_kwargs))
        extra_body = api_kwargs.get("extra_body")
        if isinstance(extra_body, dict):
            api_kwargs["extra_body"] = dict(extra_body)
        self._enforce_strata_final_answer_contract(api_kwargs)
        response = self._create_completion_retrying_empty_reasoning(
            api_kwargs,
            reason="tool follow-up",
            request_type="tool",
        )
        self._capture_generation_metrics(response)
        tools_disabled = not api_kwargs.get("tools") or api_kwargs.get("tool_choice") == "none"
        if getattr(self, "_managed_local_runtime", None) == "strata" and tools_disabled:
            if any(
                getattr(getattr(choice, "message", None), "tool_calls", None)
                for choice in (getattr(response, "choices", None) or [])
            ):
                raise RuntimeError(
                    "strata_tool_calls_when_disabled: Strata returned tool_calls for a "
                    "final-answer request with tools disabled; the unexpected tool call was not executed"
                )
        return response

    def _tool_result_retry_input(self, user_input: str, tool_calls: list[Any]) -> str:
        if not tool_calls:
            return user_input
        lines = [
            "Confirmed tool results from the previous attempt:",
            "Use these results as evidence and do not claim any other tool execution.",
            "Search candidates are not confirmed original text; only a successful media_read result is original evidence.",
        ]
        for record in tool_calls[-6:]:
            lines.append(_tool_record_retry_evidence(record, max_chars=1600))
        lines.append("")
        lines.append("Current user request:")
        lines.append(user_input)
        return "\n".join(lines)

    def _plain_empty_response_retry(
        self,
        user_input: str,
        *,
        temperature: float,
        max_tokens: Optional[int],
        context_budget: Optional[ContextBudget],
        reason: str,
        executed_tool_calls: Optional[list[Any]] = None,
    ) -> str:
        if getattr(self, "_profile_thinking_mode", None) == "always":
            # LFM2.5's reasoning-only response is a bounded failure under the
            # selected output reserve.  A second minimal prompt and a final
            # mode-dropping retry would loop on the same always-thinking
            # template and could misreport an empty final as success.
            return ""
        suppress_automatic_context = bool(
            getattr(get_turn_context(), "suppress_automatic_context", False)
        )
        if self._is_help_isolated_turn():
            # A generic minimal retry would no longer be constrained by the
            # server-grounded Guide prompt.  Let ResponseHandler classify the
            # empty result and TerminalMode surface its safe Help failure.
            return ""
        tool_calls = executed_tool_calls or []
        budget = context_budget or self._context_budget(max_tokens)
        retry_input = self._tool_result_retry_input(user_input, tool_calls)
        system_content = (
            self._system_prompt_for_budget(budget)
            if suppress_automatic_context
            else (
                "You are a helpful assistant. Answer the user's latest "
                "message directly in Japanese when the user writes Japanese."
                f"\n\n{_current_date_context()}"
            )
        )
        messages = [
            {
                "role": "system",
                "content": system_content,
            },
            {
                "role": "user",
                "content": clip_text_preserve_tail(
                    retry_input,
                    max(1000, budget.message_budget_chars - 1200),
                ),
            },
        ]
        api_kwargs = self._build_api_kwargs(
            messages,
            temperature,
            max_tokens,
            context_budget=budget,
            tools_enabled=False,
        )
        api_kwargs, _ = self._compatibility_retry_kwargs(api_kwargs)
        logger.warning(
            "[OpenAICompatibleLocalClient] Retrying empty local response with minimal prompt (%s)",
            reason,
        )
        response = self._create_completion_retrying_empty_reasoning(
            api_kwargs,
            reason=f"minimal retry after {reason}",
            request_type="retry",
        )
        self._capture_generation_metrics(response)
        message = response.choices[0].message
        content = self._message_content(message)
        if looks_like_tool_call_echo(content, self._tool_registry_names()):
            logger.warning(
                "[OpenAICompatibleLocalClient] Minimal retry returned a tool invocation echo (%s)",
                reason,
            )
            return ""
        if content:
            return self._privacy_gateway.restore(
                guard_tool_execution_claims(content, tool_calls)
            )
        if "extra_body" in api_kwargs:
            retry_kwargs = dict(api_kwargs)
            if getattr(self, "_managed_local_runtime", None) == "strata":
                # Preserve the actual request's mode, including a preceding
                # reasoning-only retry that already disabled thinking.
                retry_kwargs["extra_body"] = self._mode_extra_body_from_kwargs(api_kwargs)
            elif self._reasoning_effort_metadata():
                # Keep the selected managed-profile effort on the final
                # fallback too.  Dropping extra_body here would silently
                # change a low/medium/xhigh request into template default.
                retry_kwargs["extra_body"] = self._mode_extra_body()
            else:
                retry_kwargs.pop("extra_body", None)
            logger.warning(
                "[OpenAICompatibleLocalClient] Minimal empty-response retry returned empty; retrying with %s extra_body (%s)",
                "mode" if getattr(self, "_managed_local_runtime", None) == "strata"
                or self._reasoning_effort_metadata() else "no",
                reason,
            )
            response = self._create_completion_with_fallback(
                retry_kwargs,
                request_type="retry",
            )
            message = response.choices[0].message
            content = self._message_content(message)
            if content and not looks_like_tool_call_echo(
                content, self._tool_registry_names()
            ):
                return self._privacy_gateway.restore(
                    guard_tool_execution_claims(content, tool_calls)
                )
        return ""

    def chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        request_type: str = "chat",
        tools_enabled: bool = True,
        context_budget: Optional[ContextBudget] = None,
        fallback_user_input: Optional[str] = None,
        required_tool_name: Optional[str] = None,
        native_tool_user_input: Optional[str] = None,
        event_callback: Optional[SyncStreamEmitter] = None,
    ) -> str:
        if not bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            try:
                return self._chat_impl(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    request_type=request_type,
                    tools_enabled=tools_enabled,
                    context_budget=context_budget,
                    fallback_user_input=fallback_user_input,
                    required_tool_name=required_tool_name,
                    native_tool_user_input=native_tool_user_input,
                    event_callback=event_callback,
                )
            finally:
                self._project_request_usage_to_agent_run()
        previous_gateway = getattr(self, "_privacy_gateway", None)
        isolated_state = _snapshot_help_stream_provider_state(self)
        turn = get_turn_context()
        isolated_gateway = OutboundPrivacyGateway(
            getattr(self, "config", None),
            user_id=str(getattr(turn, "user_id", None) or ""),
            session_id=str(getattr(turn, "session_id", None) or ""),
            session_context={},
            project_metadata={},
        )
        setattr(isolated_gateway, "_aoitalk_help_isolated", True)
        self._privacy_gateway = isolated_gateway
        try:
            return self._chat_impl(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                request_type=request_type,
                tools_enabled=tools_enabled,
                context_budget=context_budget,
                fallback_user_input=fallback_user_input,
                required_tool_name=required_tool_name,
                native_tool_user_input=native_tool_user_input,
                event_callback=event_callback,
            )
        finally:
            self._project_request_usage_to_agent_run()
            self._privacy_gateway = previous_gateway
            _restore_help_stream_provider_state(self, isolated_state)

    def _chat_impl(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        request_type: str = "chat",
        tools_enabled: bool = True,
        context_budget: Optional[ContextBudget] = None,
        fallback_user_input: Optional[str] = None,
        required_tool_name: Optional[str] = None,
        native_tool_user_input: Optional[str] = None,
        event_callback: Optional[SyncStreamEmitter] = None,
    ) -> str:
        if self._is_help_isolated_turn():
            # Public compatibility callers may hand this method a complete
            # transcript.  Help accepts only the trusted system instruction
            # and the current user item; prior assistant/tool messages are
            # never sent to the local provider.
            system_message = {
                "role": "system",
                "content": AOITALK_HELP_ISOLATED_SYSTEM_PROMPT,
            }
            user_message = next(
                (
                    dict(message)
                    for message in reversed(messages)
                    if isinstance(message, dict)
                    and str(message.get("role") or "") == "user"
                ),
                {"role": "user", "content": ""},
            )
            messages = [system_message, user_message]
        api_kwargs = self._build_api_kwargs(
            messages,
            temperature,
            max_tokens,
            context_budget=context_budget,
            tools_enabled=tools_enabled,
            required_tool_name=required_tool_name,
            native_tool_user_input=native_tool_user_input,
        )
        # The provider receipt below belongs to this chat request. The
        # cumulative request ledger remains separate for terminal accounting.
        self._last_usage_records = []
        response = self._create_completion_retrying_empty_reasoning(
            api_kwargs,
            reason="chat completion",
            request_type=request_type,
        )
        self._capture_generation_metrics(response)
        choice = response.choices[0]
        if getattr(choice.message, "tool_calls", None):
            result = self._handle_tool_calls(
                messages,
                choice.message,
                api_kwargs,
                context_budget or self._context_budget(max_tokens),
                temperature=temperature,
                max_tokens=max_tokens,
                fallback_user_input=fallback_user_input,
                required_tool_name=required_tool_name,
                event_callback=event_callback,
            )
            self._record_model_transcript(messages, result)
            return self._privacy_gateway.restore(result)
        content = self._message_content(choice.message)
        self._record_provider_response_text_lengths(choice.message, content)
        if (
            not bool(getattr(get_turn_context(), "suppress_automatic_context", False))
            and fallback_user_input
            and project_progress_review_active(fallback_user_input)
        ):
            result = self._handle_tool_calls(
                messages,
                choice.message,
                api_kwargs,
                context_budget or self._context_budget(max_tokens),
                temperature=temperature,
                max_tokens=max_tokens,
                fallback_user_input=fallback_user_input,
                required_tool_name=required_tool_name,
                event_callback=event_callback,
            )
            self._record_model_transcript(messages, result)
            return self._privacy_gateway.restore(result)
        # ツール往復が無い応答でも reasoning が返っていれば thinking として配信する。
        emit_thinking(
            event_callback,
            thinking_text_from_message(choice.message),
            round_index=0,
        )
        if required_tool_name:
            logger.warning(
                "[OpenAICompatibleLocalClient] required tool %s was not called",
                required_tool_name,
            )
            return (
                "ツール実行の検証に失敗しました: "
                f"必須ツール `{required_tool_name}` が実行されませんでした。"
            )
        if content and looks_like_tool_call_echo(content, self._tool_registry_names()):
            logger.warning(
                "[OpenAICompatibleLocalClient] local model returned a tool invocation echo instead of an answer"
            )
            content = ""
        if content:
            result = guard_tool_execution_claims(content, [])
            self._record_model_transcript(messages, result)
            return self._privacy_gateway.restore(result)
        if getattr(self, "_profile_thinking_mode", None) == "always":
            return self._empty_response_fallback()
        logger.warning(
            "[OpenAICompatibleLocalClient] local model returned empty content"
        )
        if fallback_user_input:
            retry_content = self._plain_empty_response_retry(
                fallback_user_input,
                temperature=temperature,
                max_tokens=max_tokens,
                context_budget=context_budget,
                reason="chat completion",
            )
            if retry_content:
                return self._privacy_gateway.restore(retry_content)
        return self._empty_response_fallback()

    def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        suppress_automatic_context: Optional[bool] = None,
        event_callback: Optional[SyncStreamEmitter] = None,
    ) -> Iterator[str]:
        if suppress_automatic_context is None:
            suppress_automatic_context = bool(
                getattr(get_turn_context(), "suppress_automatic_context", False)
            )
        else:
            suppress_automatic_context = bool(suppress_automatic_context)
        isolated_gateway_previous = (
            getattr(self, "_privacy_gateway", None)
            if suppress_automatic_context
            else None
        )
        isolated_state = (
            _snapshot_help_stream_provider_state(self)
            if suppress_automatic_context
            else {}
        )
        return bind_context_to_iterator(
            self._stream_chat_impl(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                _suppress_automatic_context=suppress_automatic_context,
                _isolated_gateway_previous=isolated_gateway_previous,
                _isolated_state=isolated_state,
                event_callback=event_callback,
            )
        )

    def _stream_chat_impl(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        _suppress_automatic_context: bool = False,
        _isolated_gateway_previous: Any = None,
        _isolated_state: dict[str, Any] | None = None,
        event_callback: Optional[SyncStreamEmitter] = None,
    ) -> Generator[str, None, None]:
        suppress_automatic_context = bool(_suppress_automatic_context)
        isolated_gateway_previous = (
            _isolated_gateway_previous if suppress_automatic_context else None
        )
        isolated_state = dict(_isolated_state or {})
        if suppress_automatic_context:
            turn = get_turn_context()
            isolated_gateway = OutboundPrivacyGateway(
                getattr(self, "config", None),
                user_id=str(getattr(turn, "user_id", None) or ""),
                session_id=str(getattr(turn, "session_id", None) or ""),
                session_context={},
                project_metadata={},
            )
            setattr(isolated_gateway, "_aoitalk_help_isolated", True)
            self._privacy_gateway = isolated_gateway
            if self._is_help_isolated_turn():
                system_message = {
                    "role": "system",
                    "content": AOITALK_HELP_ISOLATED_SYSTEM_PROMPT,
                }
                user_message = next(
                    (
                        dict(message)
                        for message in reversed(messages)
                        if isinstance(message, dict)
                        and str(message.get("role") or "") == "user"
                    ),
                    {"role": "user", "content": ""},
                )
                messages = [system_message, user_message]
        lease_ticket = None
        stream = None
        pending_echo_probe = ""
        suppress_echo_stream = False
        self._last_stream_tool_call_echo_suppressed = False
        self._last_provider_terminal_evidence = normalize_chat_terminal(
            None, transport="chat.completions.stream",
        )
        thinking_emitted = False
        final_emitted = False
        reasoning_parser = _LeadingThinkStreamParser(
            self._stream_reasoning_markup_supported()
        )
        stream_sender_attempts_token = _LOCAL_PROVIDER_SENDER_ATTEMPTS.set(0)
        try:
            stream_tool_names = self._tool_registry_names()
        except Exception:
            stream_tool_names = set()
        try:
            _stream_generation_checkpoint()
            stream_kwargs = self._build_api_kwargs(
                messages,
                temperature,
                max_tokens,
                context_budget=self._current_context_budget,
            )
            stream_kwargs["stream"] = True
            stream_kwargs.pop("tools", None)
            stream_kwargs.pop("tool_choice", None)
            stream_kwargs.pop("response_format", None)
            stream_kwargs = self._with_stream_safe_extra_body(stream_kwargs)
            try:
                lease_ticket = self._prepare_managed_local_server_for_request()
            except Exception as exc:
                wrapped = self._wrap_preprovider_readiness_failure(
                    exc,
                    request_type="stream",
                )
                if wrapped is not exc:
                    raise wrapped from exc
                raise
            _stream_generation_checkpoint()
            stream = self._execute_completion_request(
                stream_kwargs,
                request_type="stream",
            )
            stream_kwargs = dict(
                getattr(self, "_last_privacy_payload", stream_kwargs)
            )
            self._capture_context_request(stream_kwargs, reason="stream")
            # Provider deltas are queued in arrival order and drained through
            # the same reasoning/final/echo logic.  While the consumer keeps
            # up, every delta is drained immediately (chunk-by-chunk stream).
            # Once one drain is slow (per-event durable audit/WebSocket relay),
            # later deltas are coalesced for a short window so a
            # reasoning-heavy turn cannot fall minutes behind the provider.
            queued_segments: list[list[str]] = []
            hold_seconds = 0.0
            last_drain_end = 0.0

            def _queue_segment(kind: str, text: str) -> None:
                if queued_segments and queued_segments[-1][0] == kind:
                    queued_segments[-1][1] += text
                else:
                    queued_segments.append([kind, text])

            def _drain_segments(
                segments: list[list[str]],
            ) -> Generator[str, None, None]:
                nonlocal thinking_emitted, final_emitted
                nonlocal pending_echo_probe, suppress_echo_stream
                for kind, segment_text in segments:
                    if kind == "reasoning":
                        if not segment_text.strip():
                            continue
                        # Explicit reasoning fields are authoritative evidence
                        # that this endpoint supports a reasoning channel.  It
                        # also enables leading-tag parsing for a generic client
                        # that advertises the field without a managed profile.
                        reasoning_parser.enable()
                        thinking_emitted = True
                        emit_thinking(
                            event_callback,
                            segment_text,
                            round_index=0,
                        )
                        _stream_generation_checkpoint()
                        continue
                    reasoning_parts, final_parts = reasoning_parser.feed(
                        segment_text
                    )
                    for reasoning_part in reasoning_parts:
                        thinking_emitted = True
                        emit_thinking(
                            event_callback,
                            reasoning_part,
                            round_index=0,
                        )
                        _stream_generation_checkpoint()
                    for raw_final_part in final_parts:
                        if raw_final_part.strip():
                            final_emitted = True
                        content = self._privacy_gateway.restore_aliases(
                            raw_final_part
                        )
                        if suppress_echo_stream:
                            continue
                        candidate = pending_echo_probe + content
                        probe_needed = _stream_echo_probe_needed(
                            candidate,
                            stream_tool_names,
                        )
                        if pending_echo_probe and not probe_needed:
                            # The suspicious prefix resolved to ordinary prose
                            # (for example, "tool usage").  Flush it promptly
                            # instead of turning normal English into a full
                            # response buffer.
                            yield pending_echo_probe
                            yield content
                            pending_echo_probe = ""
                        elif probe_needed:
                            if (
                                looks_like_tool_call_echo(
                                    candidate,
                                    stream_tool_names,
                                )
                                and candidate.strip()
                                not in {str(name) for name in stream_tool_names}
                            ):
                                # Pure tool-call/log echoes are internal model
                                # chatter and must never reach the UI.
                                pending_echo_probe = ""
                                suppress_echo_stream = True
                            elif (
                                len(candidate) < STREAM_ECHO_PROBE_LIMIT
                                or _stream_echo_call_shaped(
                                    candidate,
                                    stream_tool_names,
                                )
                            ):
                                pending_echo_probe = candidate
                            else:
                                # Only the suspicious leading fragment was
                                # buffered; ordinary long prose resumes normal
                                # chunk-by-chunk streaming after this point.
                                if pending_echo_probe:
                                    yield pending_echo_probe
                                pending_echo_probe = ""
                                yield content
                        else:
                            yield content

            for chunk in stream:
                # Reasoning-only deltas are intentionally not yielded as
                # response text, so the outer relay cannot observe a stop
                # while the provider is spending its reserve on thinking.
                _stream_generation_checkpoint()
                terminal = normalize_chat_terminal(chunk, transport="chat.completions.stream")
                observed = getattr(self, "_last_provider_terminal_evidence", None) or {}
                if terminal["finish_reason_present"] and observed.get("state") not in {"incomplete", "interrupted"}:
                    self._last_provider_terminal_evidence = terminal
                delta = (
                    chunk.choices[0].delta
                    if getattr(chunk, "choices", None)
                    else None
                )
                if delta is not None:
                    reasoning_text = _stream_reasoning_delta_text(
                        delta,
                        strip=False,
                    )
                    if reasoning_text:
                        _queue_segment("reasoning", reasoning_text)
                    raw_content = _message_field(delta, "content")
                    if isinstance(raw_content, str) and raw_content:
                        _queue_segment("content", raw_content)
                previous_usage = dict(getattr(self, "_last_usage", {}) or {})
                captured_usage = self._capture_generation_metrics(chunk)
                # Some servers attach usage only to an intermediate/final chunk;
                # do not erase the last confirmed metrics when a content-only
                # chunk follows it.
                if not captured_usage and previous_usage:
                    self._last_usage = previous_usage
                if not queued_segments:
                    continue
                drain_start = time.monotonic()
                if drain_start - last_drain_end < hold_seconds:
                    continue
                segments = list(queued_segments)
                queued_segments.clear()
                yield from _drain_segments(segments)
                last_drain_end = time.monotonic()
                hold_seconds = (
                    STREAM_SLOW_CONSUMER_COALESCE_SECONDS
                    if last_drain_end - drain_start
                    >= STREAM_SLOW_CONSUMER_THRESHOLD_SECONDS
                    else 0.0
                )
            terminal = getattr(self, "_last_provider_terminal_evidence", None)
            if terminal and terminal["state"] in {"incomplete", "interrupted"}:
                raise ModelOutputIncompleteError(
                    terminal["finish_reason"], transport="chat.completions.stream",
                )
            if queued_segments:
                segments = list(queued_segments)
                queued_segments.clear()
                yield from _drain_segments(segments)
            reasoning_parts, final_parts = reasoning_parser.finish()
            for reasoning_part in reasoning_parts:
                thinking_emitted = True
                emit_thinking(
                    event_callback,
                    reasoning_part,
                    round_index=0,
                )
                _stream_generation_checkpoint()
            for raw_final_part in final_parts:
                if raw_final_part.strip():
                    final_emitted = True
                content = self._privacy_gateway.restore_aliases(raw_final_part)
                if suppress_echo_stream:
                    continue
                candidate = pending_echo_probe + content
                probe_needed = _stream_echo_probe_needed(
                    candidate,
                    stream_tool_names,
                )
                if pending_echo_probe and not probe_needed:
                    yield pending_echo_probe
                    yield content
                    pending_echo_probe = ""
                elif probe_needed:
                    if (
                        looks_like_tool_call_echo(candidate, stream_tool_names)
                        and candidate.strip()
                        not in {str(name) for name in stream_tool_names}
                    ):
                        pending_echo_probe = ""
                        suppress_echo_stream = True
                    elif (
                        len(candidate) < STREAM_ECHO_PROBE_LIMIT
                        or _stream_echo_call_shaped(candidate, stream_tool_names)
                    ):
                        pending_echo_probe = candidate
                    else:
                        if pending_echo_probe:
                            yield pending_echo_probe
                        pending_echo_probe = ""
                        yield content
                else:
                    yield content
            _stream_generation_checkpoint()
            if thinking_emitted and not final_emitted and not pending_echo_probe:
                # Reasoning alone is not a response: surface the same bounded
                # empty-response failure the non-stream path reports.
                yield LOCAL_MODEL_EMPTY_RESPONSE
            pending_is_tool_call = bool(pending_echo_probe) and (
                _stream_echo_call_shaped(
                    pending_echo_probe,
                    stream_tool_names,
                )
                or looks_like_tool_call_echo(
                    pending_echo_probe,
                    stream_tool_names,
                )
            )
            # The plain stream carries no tool schema, so a withheld call is
            # never executed here.  Let the caller decide whether to answer
            # through the native tool loop instead of finishing silently.
            self._last_stream_tool_call_echo_suppressed = bool(
                suppress_echo_stream or pending_is_tool_call
            )
            if pending_echo_probe and not pending_is_tool_call:
                yield pending_echo_probe
        except (httpx.TransportError, APIError):
            self._last_provider_terminal_evidence = interrupted_terminal("chat.completions.stream")
            raise
        finally:
            try:
                close = getattr(stream, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        # Closing a provider stream is best-effort cleanup. It
                        # must never prevent the generation lease from being
                        # released, nor mask the original cancellation/error.
                        logger.debug(
                            "local provider stream close failed",
                            exc_info=True,
                        )
            finally:
                try:
                    if lease_ticket is not None:
                        lease_ticket.release()
                finally:
                    _LOCAL_PROVIDER_SENDER_ATTEMPTS.reset(
                        stream_sender_attempts_token
                    )
                    if suppress_automatic_context:
                        self._privacy_gateway = isolated_gateway_previous
                        _restore_help_stream_provider_state(self, isolated_state)

    def _handle_tool_calls(
        self,
        messages: List[Dict[str, Any]],
        assistant_message: Any,
        api_kwargs: Dict[str, Any],
        context_budget: ContextBudget,
        *,
        temperature: float,
        max_tokens: Optional[int],
        fallback_user_input: Optional[str] = None,
        required_tool_name: Optional[str] = None,
        max_rounds: int = 5,
        event_callback: Optional[SyncStreamEmitter] = None,
    ) -> str:
        if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            raise RuntimeError("AoiTalk Help turns expose no provider tools")
        effective_max_rounds = max(
            max_rounds,
            agentic_max_rounds(self, fallback_user_input),
        )

        # llama.cpp requires the initial grounding tool to be selected with a
        # native ``tool_choice=required`` request.  Its initial schema is
        # intentionally narrowed to that single tool, but reusing those
        # kwargs for the follow-up would permanently hide mutation tools.  The
        # assistant message already contains the required read call, so
        # rebuild a normal effective schema for the loop's subsequent calls.
        loop_api_kwargs = api_kwargs
        if required_tool_name and self._is_llama_cpp_server():
            loop_api_kwargs = self._build_api_kwargs(
                messages,
                temperature,
                max_tokens,
                context_budget=context_budget,
                tools_enabled=True,
                required_tool_name=None,
                native_tool_user_input=fallback_user_input,
            )
        loop_event_callback = event_callback
        if event_callback is not None:
            active_tool_started: dict[str, float] = {}

            def _timed_event(event_type: str, data: dict[str, Any]) -> None:
                payload = dict(data or {})
                operation_id = str(
                    payload.get("operation_id")
                    or payload.get("tool_call_id")
                    or ""
                ).strip()
                tool_name = str(
                    payload.get("tool") or payload.get("tool_name") or ""
                ).strip()
                key = operation_id or tool_name
                if event_type == "tool_start":
                    started_at = time.monotonic()
                    if key:
                        active_tool_started[key] = started_at
                    _emit_local_timing(
                        "tool",
                        "start",
                        callback=event_callback,
                        tool=tool_name or None,
                        tool_call_id=operation_id or None,
                    )
                elif event_type == "tool_end":
                    started_at = active_tool_started.pop(key, None) if key else None
                    if started_at is not None:
                        payload.setdefault(
                            "duration_ms",
                            max(0, int(round((time.monotonic() - started_at) * 1000))),
                        )
                event_callback(event_type, payload)
                if event_type == "tool_end":
                    _emit_local_timing(
                        "tool",
                        "end",
                        started_at=started_at,
                        callback=event_callback,
                        tool=tool_name or None,
                        tool_call_id=operation_id or None,
                    )

            loop_event_callback = _timed_event
        try:
            result = run_openai_tool_call_loop(
                initial_messages=messages,
                assistant_message=assistant_message,
                api_kwargs=loop_api_kwargs,
                registry=filtered_registry_for_client(self, self._tool_registry),
                create_completion=self._tool_loop_completion,
                log_prefix="OpenAICompatibleLocalClient",
                max_rounds=effective_max_rounds,
                return_result=True,
                max_tool_result_chars=context_budget.tool_result_chars,
                message_content=self._message_content,
                config=self.config,
                user_input=fallback_user_input,
                budget_client=self,
                require_project_context_read=(
                    self._requires_project_context_read(fallback_user_input)
                    and not self._project_context_read_already_satisfied()
                ),
                skip_final_response_check_on_empty=True,
                event_callback=loop_event_callback,
                restore_tool_arguments=self._privacy_gateway.restore_tool_arguments,
                tool_call_identity_check=build_durable_tool_call_identity_check(
                    filtered_registry_for_client(self, self._tool_registry)
                ),
            )
        except AgentDialogueInputPending as pending:
            # The shared loop has already projected its request-local usage
            # ledger. Preserve prior tool names/statuses for the terminal
            # AgentRun audit without carrying arguments or provider output.
            self._project_request_usage_to_agent_run()
            safe_records = getattr(pending, "_native_tool_records", None)
            if isinstance(safe_records, list):
                self._last_tool_calls.extend(dict(item) for item in safe_records)
                self._last_audit_tool_calls.extend(
                    dict(item) for item in safe_records
                )
            raise
        except PlanningInteractionTerminated:
            # Plan approval/timeout/cancel is typed control flow as well. Keep
            # the request usage projection, but never turn it into fallback
            # assistant text or attach question-only evidence.
            self._project_request_usage_to_agent_run()
            raise
        usage_records = list(getattr(result, "usage_records", None) or [])
        if usage_records:
            self._last_usage_records = [
                dict(item) for item in usage_records if isinstance(item, dict)
            ]
        self._last_tool_calls.extend(result.tool_calls)
        self._last_tool_loop_messages = [
            dict(message) for message in getattr(result, "messages", [])
        ]
        execution_records = list(getattr(result, "audit_tool_calls", None) or [])
        self._last_audit_tool_calls.extend(execution_records)
        self._last_tool_loop_completion_confirmed = tool_loop_completion_confirmed(
            execution_records,
            result.final_output,
            stopped_reason=getattr(result, "stopped_reason", None),
        )
        final_output = result.final_output
        if final_output and looks_like_tool_call_echo(
            final_output, self._tool_registry_names()
        ):
            # A tool invocation/log line is not an answer.  Answer again
            # from the confirmed tool results below instead of showing it.
            logger.warning(
                "[OpenAICompatibleLocalClient] tool call loop returned a tool invocation echo instead of an answer"
            )
            final_output = ""
        if final_output:
            return self._privacy_gateway.restore(
                guard_tool_execution_claims(final_output, result.tool_calls)
            )
        logger.warning(
            "[OpenAICompatibleLocalClient] tool call loop returned empty content"
        )
        if fallback_user_input:
            retry_user_input = self._tool_result_retry_input(
                fallback_user_input,
                result.tool_calls,
            )
            retry_content = self._plain_empty_response_retry(
                retry_user_input,
                temperature=temperature,
                max_tokens=max_tokens,
                context_budget=context_budget,
                reason="tool call loop",
                executed_tool_calls=result.tool_calls,
            )
            if retry_content:
                return self._privacy_gateway.restore(retry_content)
        return self._empty_response_fallback()

    def _run_agentic_review_once(
        self,
        prompt: str,
        *,
        temperature: float,
        max_tokens: Optional[int],
        context_budget: ContextBudget,
        user_input: str,
    ) -> str:
        if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            raise RuntimeError(
                "OpenAI-compatible review loop is disabled for isolated Help turns"
            )
        review_messages = [
            {
                "role": "system",
                "content": (
                    "You are a careful AoiTalk completion verifier. "
                    "Return exactly one JSON review object and do not call tools."
                ),
            },
            {"role": "user", "content": prompt},
        ]
        # Empty-reasoning/compatibility retries inside this sidecar use the
        # generic request_type="retry". They still cannot replace the work
        # request's authoritative terminal receipt.
        token = _LOCAL_PROVIDER_TERMINAL_TRACKING.set(False)
        try:
            return self.chat(
                review_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                request_type="review",
                tools_enabled=False,
                context_budget=context_budget,
                fallback_user_input=user_input,
                native_tool_user_input=user_input,
            )
        finally:
            _LOCAL_PROVIDER_TERMINAL_TRACKING.reset(token)

    def _run_agentic_continuation_once(
        self,
        prompt: str,
        *,
        temperature: float,
        max_tokens: Optional[int],
        context_budget: ContextBudget,
        user_input: str,
        event_callback: Optional[SyncStreamEmitter] = None,
        system_content: str = "",
    ) -> str:
        if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            raise RuntimeError(
                "OpenAI-compatible continuation is disabled for isolated Help turns"
            )
        continuation_messages = [
            {
                "role": "system",
                "content": (
                    system_content
                    or self._current_turn_system_content
                    or (
                        self._system_prompt_for_budget(context_budget)
                        + "\n\n"
                        + _current_date_context()
                    )
                ),
            },
            {"role": "user", "content": prompt},
        ]
        required_tool_name = self._required_tool_name(user_input)
        return self.chat(
            continuation_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            request_type="continuation",
            tools_enabled=True,
            context_budget=context_budget,
            fallback_user_input=user_input,
            required_tool_name=required_tool_name,
            native_tool_user_input=user_input,
            event_callback=event_callback,
        )

    def generate_response(
        self,
        user_input: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        image_data: Optional[Dict[str, Any]] = None,
        stream_callback: Any = None,
    ) -> Union[str, Generator[str, None, None]]:
        if max_tokens in (None, ""):
            max_tokens = self._configured_output_limit()
        projected_max_tokens = dialogue_provider_wire_max_output_tokens(
            self,
            user_input=user_input,
            requested=max_tokens,
        )
        if projected_max_tokens is not None:
            if projected_max_tokens <= 0:
                raise ValueError("AgentDialogue provider budget is exhausted")
            max_tokens = projected_max_tokens
        suppress_automatic_context = bool(
            getattr(get_turn_context(), "suppress_automatic_context", False)
        )
        isolated_gateway_previous = None
        isolated_gateway_active = False
        if suppress_automatic_context:
            isolated_gateway_previous = getattr(self, "_privacy_gateway", None)
            isolated_gateway_active = True
        isolated_provider_state = (
            _snapshot_help_stream_provider_state(self)
            if suppress_automatic_context
            else {}
        )
        # A compatibility tool loop publishes its generation failure through
        # a task-local ContextVar.  Reset it at the owning turn boundary so a
        # terminal circuit that skips the owner review cannot poison the next
        # request in the same worker.
        compatible_generation_failure_token = (
            set_current_compatible_generation_failure(None)
        )
        no_progress_guard_scope = activate_tool_no_progress_guard(
            current_tool_no_progress_guard() or ToolNoProgressGuard()
        )
        no_progress_guard_scope.__enter__()
        self._last_generation_metrics = None
        self._last_generation_failure = None
        self._last_provider_terminal_evidence = None
        self._last_context_snapshots = []
        self._last_memory_selection_observation = None
        self._last_tool_calls = []
        self._last_agentic_events = []
        self._last_model_transcript = []
        self._last_usage = {}
        # Usage/accounting is request-local. Never let a previous Dialogue
        # Run satisfy a later terminal event after a typed pause or retry.
        self._agent_dialogue_usage = {}
        self._last_usage_records = []
        self._reset_request_usage_ledger()
        self._last_tool_loop_messages = []
        self._last_tool_loop_completion_confirmed = False
        self._last_audit_tool_calls = []
        self._current_turn_system_content = ""
        project_token = None
        specialist_provider_token = set_runtime_specialist_provider(
            "openai_compatible_local"
        )
        tool_policy_token = set_current_user_input(user_input)
        policy = get_client_generation_policy(self)
        generation_policy_token = set_current_generation_policy(policy)
        context_budget: Optional[ContextBudget] = None
        project_context: Optional[dict[str, Any]] = None
        stream_lifecycle_transferred = False
        memory_callback_token = _MEMORY_SELECTION_STREAM_CALLBACK.set(stream_callback)
        timing_id_token = _LOCAL_TIMING_ID.set(uuid4().hex)
        timing_emitter = make_sync_stream_emitter(stream_callback)
        timing_callback_token = _LOCAL_TIMING_CALLBACK.set(timing_emitter)
        sender_attempts_token = _LOCAL_PROVIDER_SENDER_ATTEMPTS.set(0)
        turn_active_token = _LOCAL_PROVIDER_TURN_ACTIVE.set(True)
        try:
            context_budget = self._context_budget(max_tokens)
            self._current_context_budget = context_budget
            input_started_at = time.monotonic()
            _emit_local_timing(
                "input",
                "start",
                model=self.model_name,
            )
            project_context = self._resolve_project_context_sync()
            project_token = set_runtime_project_context(project_context)
            self._privacy_project_metadata = (
                dict((project_context or {}).get("metadata") or {})
                if isinstance(project_context, dict)
                and isinstance((project_context or {}).get("metadata"), dict)
                else {}
            )
            self._sync_privacy_gateway()
            messages, tool_hint_context = self._build_model_messages_for_budget(
                user_input,
                project_context,
                context_budget,
            )
            _emit_local_timing(
                "input",
                "end",
                started_at=input_started_at,
                model=self.model_name,
                input_chars=len(str(user_input or "")),
                message_count=len(messages),
                tool_hint_chars=len(str(tool_hint_context or "")),
            )
            if image_data and messages:
                messages[-1]["content"] = openai_content_parts(
                    str(messages[-1].get("content") or ""),
                    image_data,
                )
            self._current_turn_system_content = str(
                messages[0].get("content") or ""
            )
            required_tool_name = self._required_tool_name(user_input)
            if stream:
                if self._stream_should_use_tool_loop(
                    user_input,
                    required_tool_name,
                ):
                    # ``stream_chat`` is a transport-only plain stream and
                    # intentionally removes native tool fields.  Retrieval
                    # requests must use the existing tool loop first, then
                    # expose its verified final text through the stream API.
                    turn_event_emitter = timing_emitter
                    response_text = self.chat(
                        messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        tools_enabled=True,
                        context_budget=context_budget,
                        fallback_user_input=user_input,
                        required_tool_name=required_tool_name,
                        native_tool_user_input=user_input,
                        event_callback=turn_event_emitter,
                    )

                    def _verified_tool_stream() -> Generator[str, None, None]:
                        if response_text:
                            yield response_text

                    return _verified_tool_stream()
                stream_method = self._stream_response
                # Keep older rolling workers that monkey-patch the private
                # helper on the four-argument signature, while passing the
                # authoritative flag to the current implementation.  A
                # signature check avoids swallowing a real provider TypeError.
                try:
                    stream_signature = inspect.signature(stream_method)
                    stream_parameters = stream_signature.parameters
                    stream_accepts_kwargs = any(
                        parameter.kind == inspect.Parameter.VAR_KEYWORD
                        for parameter in stream_parameters.values()
                    )
                except (TypeError, ValueError):
                    stream_parameters = {}
                    stream_accepts_kwargs = True
                suppress_parameter = stream_parameters.get(
                    "suppress_automatic_context"
                )
                event_callback_parameter = stream_parameters.get("event_callback")
                stream_accepts_suppress = (
                    suppress_parameter is not None
                    and suppress_parameter.kind
                    in {
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.KEYWORD_ONLY,
                    }
                )
                stream_accepts_event_callback = (
                    event_callback_parameter is not None
                    and event_callback_parameter.kind
                    in {
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.KEYWORD_ONLY,
                    }
                )
                stream_kwargs = {}
                if stream_accepts_kwargs or stream_accepts_suppress:
                    stream_kwargs["suppress_automatic_context"] = (
                        suppress_automatic_context
                    )
                if stream_accepts_kwargs or stream_accepts_event_callback:
                    stream_kwargs["event_callback"] = timing_emitter
                if (
                    stream_kwargs
                ):
                    stream_iterator = stream_method(
                        messages,
                        temperature,
                        max_tokens,
                        user_input,
                        **stream_kwargs,
                    )
                else:
                    stream_iterator = stream_method(
                        messages,
                        temperature,
                        max_tokens,
                        user_input,
                    )

                # The stream may be consumed on another thread.  Release the
                # setup ContextVar tokens in this caller context before
                # copying it, then bind the same request values in a private
                # stream context whose tokens can be reset during finalization.
                if project_token is not None:
                    reset_runtime_project_context(project_token)
                    project_token = None
                reset_runtime_specialist_provider(specialist_provider_token)
                specialist_provider_token = None
                reset_current_generation_policy(generation_policy_token)
                generation_policy_token = None
                reset_current_user_input(tool_policy_token)
                tool_policy_token = None

                stream_context = contextvars.copy_context()

                def _bind_stream_context():
                    return (
                        set_runtime_project_context(project_context),
                        set_runtime_specialist_provider(
                            "openai_compatible_local"
                        ),
                        set_current_generation_policy(policy),
                        set_current_user_input(user_input),
                    )

                (
                    stream_project_token,
                    stream_specialist_provider_token,
                    stream_generation_policy_token,
                    stream_tool_policy_token,
                ) = stream_context.run(_bind_stream_context)

                def _finalize_stream() -> None:
                    # Preserve the non-stream cleanup order: restore the
                    # request ContextVars first, capture the completed
                    # request/bundle observation, then clear turn-local state.
                    reset_runtime_project_context(stream_project_token)
                    reset_runtime_specialist_provider(
                        stream_specialist_provider_token
                    )
                    reset_current_generation_policy(
                        stream_generation_policy_token
                    )
                    reset_current_user_input(stream_tool_policy_token)
                    try:
                        if not suppress_automatic_context:
                            capture_context_manifest_before_context_clear(self)
                    finally:
                        self._current_context_bundle = None
                        self._current_prompt_stable_context = []
                        self._current_prompt_dynamic_context = []
                        self._current_project_cache_revision = ""
                        self._current_context_budget = None
                        self._current_tool_hint_context = ""
                        self._current_tool_hint_catalog_names = ()
                        self._current_tool_hint_carrier_prefix_sha256 = None
                        if isolated_gateway_active:
                            self._privacy_gateway = isolated_gateway_previous
                        if suppress_automatic_context:
                            _restore_help_stream_provider_state(
                                self,
                                isolated_provider_state,
                            )

                lifecycle_stream = _LifecycleStream(
                    stream_iterator,
                    context=stream_context,
                    on_close=_finalize_stream,
                )
                stream_lifecycle_transferred = True
                return lifecycle_stream
            turn_event_emitter = timing_emitter
            response_text = self.chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                tools_enabled=True,
                context_budget=context_budget,
                fallback_user_input=user_input,
                required_tool_name=required_tool_name,
                native_tool_user_input=user_input,
                event_callback=turn_event_emitter,
            )

            def _agentic_event_callback(event_type: str, data: Dict[str, Any]) -> None:
                event_data = dict(data or {})
                event_data["event_type"] = event_type
                self._last_agentic_events.append(event_data)
                if stream_callback:
                    self._run_async_sync(stream_callback(event_type, data))

            if (
                not suppress_automatic_context
                and get_current_compatible_generation_failure() is None
                and self._should_run_agentic_completion_review()
            ):
                response_text = run_agentic_completion_loop_sync(
                    client=self,
                    run_once=lambda prompt: self._run_agentic_continuation_once(
                        prompt,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        context_budget=context_budget,
                        user_input=user_input,
                        event_callback=turn_event_emitter,
                        system_content=self._current_turn_system_content,
                    ),
                    run_review_once=lambda prompt: self._run_agentic_review_once(
                        prompt,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        context_budget=context_budget,
                        user_input=user_input,
                    ),
                    run_continuation_once=lambda prompt: self._run_agentic_continuation_once(
                        prompt,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        context_budget=context_budget,
                        user_input=user_input,
                        event_callback=turn_event_emitter,
                        system_content=self._current_turn_system_content,
                    ),
                    context=render_messages_for_review(messages),
                    user_input=user_input,
                    initial_response=response_text,
                    event_callback=_agentic_event_callback,
                    tool_evidence_provider=self._format_last_tool_calls_evidence,
                    completion_confirmed_provider=lambda: self._last_tool_loop_completion_confirmed,
                    audit_tool_calls_provider=lambda: list(self._last_audit_tool_calls),
                )
            self._record_model_transcript(messages, response_text)
            if not suppress_automatic_context:
                self.history_manager.add_message("user", user_input)
                self.history_manager.add_message("assistant", response_text)
            self._project_request_usage_to_agent_run()
            return response_text
        except LocalProviderNotStartedError:
            self._project_request_usage_to_agent_run()
            raise
        except AgentDialogueInputPending:
            # A worker question has already been durably persisted and must
            # reach TerminalMode's typed pause boundary unchanged.
            self._project_request_usage_to_agent_run()
            raise
        except PlanningInteractionTerminated:
            self._project_request_usage_to_agent_run()
            raise
        except GenerationCancelled:
            # A user stop is typed control flow; never turn it into fallback
            # text or let the old turn look successful.
            raise
        except GenerationInterrupted:
            raise
        except Exception as exc:
            self._project_request_usage_to_agent_run()
            self._record_generation_failure(exc)
            if isinstance(exc, ModelOutputIncompleteError) and exc.request_type == "review":
                # An incomplete verifier sample cannot authorize another tool
                # turn. Keep the work receipt intact and surface this separate
                # typed failure to task settlement instead of fallback prose.
                raise
            fallback = self._get_error_response(exc)
            if _is_local_model_loading_error(exc):
                logger.warning(
                    "[OpenAICompatibleLocalClient] local model is still loading: %s",
                    exc,
                )
            elif _is_context_overflow_error(exc):
                self._reduce_context_budget_after_overflow()
                logger.warning(
                    "[OpenAICompatibleLocalClient] local model context overflow: %s",
                    exc,
                )
                if not suppress_automatic_context and not stream:
                    try:
                        retry_text = self._retry_after_context_overflow(
                            user_input=user_input,
                            project_context=project_context,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            required_tool_name=required_tool_name,
                        )
                        if retry_text:
                            self.history_manager.add_message("user", user_input)
                            self.history_manager.add_message("assistant", retry_text)
                            self._last_generation_failure = None
                            set_current_compatible_generation_failure(None)
                            return retry_text
                    except Exception as retry_exc:
                        logger.warning(
                            "[OpenAICompatibleLocalClient] compact overflow retry failed: %s",
                            retry_exc,
                        )
            else:
                logger.error(
                    "[OpenAICompatibleLocalClient] generation failed: %s",
                    exc,
                    exc_info=True,
                )
            if suppress_automatic_context:
                # Do not turn a transport/provider failure into an ordinary
                # character fallback: it would be ungrounded by the Guide.
                return ""
            self.history_manager.add_message("user", user_input)
            self.history_manager.add_message("assistant", fallback)
            if stream:
                return iter([fallback])
            return fallback
        finally:
            no_progress_guard_scope.__exit__(None, None, None)
            reset_current_compatible_generation_failure(
                compatible_generation_failure_token
            )
            if not stream_lifecycle_transferred:
                self._project_request_usage_to_agent_run()
            _LOCAL_PROVIDER_TURN_ACTIVE.reset(turn_active_token)
            _LOCAL_PROVIDER_SENDER_ATTEMPTS.reset(sender_attempts_token)
            _LOCAL_TIMING_CALLBACK.reset(timing_callback_token)
            _LOCAL_TIMING_ID.reset(timing_id_token)
            _MEMORY_SELECTION_STREAM_CALLBACK.reset(memory_callback_token)
            if not stream_lifecycle_transferred:
                if project_token is not None:
                    reset_runtime_project_context(project_token)
                reset_runtime_specialist_provider(specialist_provider_token)
                reset_current_generation_policy(generation_policy_token)
                reset_current_user_input(tool_policy_token)
                if not suppress_automatic_context:
                    capture_context_manifest_before_context_clear(self)
                self._current_context_bundle = None
                self._current_prompt_stable_context = []
                self._current_prompt_dynamic_context = []
                self._current_project_cache_revision = ""
                self._current_context_budget = None
                self._current_tool_hint_context = ""
                self._current_tool_hint_catalog_names = ()
                self._current_tool_hint_carrier_prefix_sha256 = None
                if isolated_gateway_active:
                    self._privacy_gateway = isolated_gateway_previous
                # A custom/legacy stream helper can fail while being created,
                # before ownership transfers to ``_LifecycleStream``.  Keep
                # that setup failure from erasing the prior ordinary ledgers.
                if suppress_automatic_context:
                    _restore_help_stream_provider_state(
                        self,
                        isolated_provider_state,
                    )

    def generate_title(
        self,
        prompt: str,
        temperature: float = 0.2,
        max_tokens: Optional[int] = 64,
    ) -> str:
        """Generate a session title without normal chat context or history."""
        self._last_generation_metrics = None
        context_budget = self._context_budget(max_tokens)
        messages = [
            {"role": "system", "content": TITLE_GENERATION_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        api_kwargs = self._build_api_kwargs(
            messages,
            temperature,
            max_tokens,
            context_budget=context_budget,
            tools_enabled=False,
        )
        api_kwargs.pop("response_format", None)
        response = self._create_completion_retrying_empty_reasoning(
            api_kwargs,
            reason="title generation",
            request_type="title",
        )
        self._capture_generation_metrics(response)
        return self._message_content(response.choices[0].message)

    def _generate_plain_text(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
        request_type: str = "plain",
    ) -> str:
        """Generate text without normal chat context, tools, or history."""
        self._last_generation_metrics = None
        config = getattr(self, "config", None)
        output_limit = config.get("runtime.target_max_output_tokens", 1024) if config else 1024
        output_limit = min(1024, max(1, int(output_limit or 1024)))
        context_budget = self._context_budget(output_limit)
        messages = [
            {
                "role": "system",
                "content": system_prompt
                or "You are a concise assistant. Do not call tools or modify files.",
            },
            {"role": "user", "content": prompt},
        ]
        api_kwargs = self._build_api_kwargs(
            messages,
            temperature=0.2,
            max_tokens=output_limit,
            context_budget=context_budget,
            tools_enabled=False,
        )
        api_kwargs.pop("response_format", None)
        response = self._create_completion_retrying_empty_reasoning(
            api_kwargs,
            reason="ephemeral text generation",
            request_type=request_type,
        )
        self._capture_generation_metrics(response)
        return self._message_content(response.choices[0].message)

    async def generate_plain_text_async(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
    ) -> str:
        """Generate text without mutating normal conversation history."""
        if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            system_prompt = AOITALK_HELP_ISOLATED_SYSTEM_PROMPT
        # Cancellation of to_thread only stops the awaiter. Keep request-owned
        # leases until the synchronous request's finally has actually run.
        workers = {event for event in getattr(self, "_ephemeral_workers", ()) if not event.is_set()}
        completed = threading.Event()
        workers.add(completed)
        self._ephemeral_workers = workers
        budget = current_generation_budget.get()
        if budget is not None:
            budget.worker_tracked = True

        def generate() -> str:
            if budget is not None:
                budget.worker_pending.set()
            try:
                if budget is not None:
                    budget.checkpoint()
                return self._generate_plain_text(
                    prompt, system_prompt=system_prompt, request_type="plain"
                )
            finally:
                if budget is not None:
                    budget.worker_pending.clear()
                completed.set()
                self._release_requested_local_runtime_consumer_lease()

        return await asyncio.to_thread(generate)

    def _generate_memory_extraction_text(
        self,
        prompt: str,
        *,
        system_prompt: str,
    ) -> str:
        """Generate deterministic JSON-oriented memory text.

        Reasoning-first local models can spend a 1024-token generic utility
        budget entirely inside ``reasoning_content`` and return an empty final
        answer. Memory extraction does not benefit from hidden chain-of-thought,
        so request the chat template's non-thinking mode first with a bounded
        4096-token output. ``_create_completion_with_fallback`` removes the
        optional extra body and retries normally when a runtime does not support
        that switch.
        """

        self._last_generation_metrics = None
        context_budget = self._context_budget(MEMORY_EXTRACTION_MAX_TOKENS)
        output_tokens = context_budget.response_tokens
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]
        api_kwargs = self._build_api_kwargs(
            messages,
            temperature=0.0,
            max_tokens=output_tokens,
            context_budget=context_budget,
            tools_enabled=False,
        )
        api_kwargs.pop("response_format", None)
        api_kwargs = self._set_chat_template_thinking(api_kwargs, False)
        extra_body = copy.deepcopy(api_kwargs.get("extra_body") or {})
        template_kwargs = extra_body.get("chat_template_kwargs")
        if isinstance(template_kwargs, dict):
            template_kwargs.pop("reasoning_effort", None)
        extra_body.pop("reasoning_effort", None)
        api_kwargs["extra_body"] = extra_body

        response = self._create_completion_with_fallback(
            api_kwargs,
            request_type="memory_extraction",
        )
        self._capture_generation_metrics(response)
        content = self._message_content(response.choices[0].message)
        if content:
            return content
        if getattr(self, "_profile_thinking_mode", None) == "always":
            # The official template cannot switch to a non-thinking JSON
            # branch.  Keep the empty result typed as a failed extraction
            # instead of issuing another identical reasoning request.
            return content

        # A runtime may accept but ignore enable_thinking=false. Retry once with
        # its native profile at the larger budget rather than treating a hidden
        # reasoning trace as authoritative JSON.
        retry_kwargs = self._build_api_kwargs(
            messages,
            temperature=0.0,
            max_tokens=output_tokens,
            context_budget=context_budget,
            tools_enabled=False,
        )
        retry_kwargs.pop("response_format", None)
        response = self._create_completion_with_fallback(
            retry_kwargs,
            request_type="memory_extraction_retry",
        )
        self._capture_generation_metrics(response)
        return self._message_content(response.choices[0].message)

    async def generate_memory_extraction_async(
        self,
        prompt: str,
        *,
        system_prompt: str,
    ) -> str:
        """Generate memory extraction text without mutating chat history."""

        if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            system_prompt = AOITALK_HELP_ISOLATED_SYSTEM_PROMPT
        # Cleanup can be requested while this worker is still using the local
        # endpoint. Track it alongside plain-text workers so the consumer
        # lease cannot be released between request start and transport exit.
        completed = threading.Event()
        workers = {
            event
            for event in getattr(self, "_ephemeral_workers", ())
            if not event.is_set()
        }
        workers.add(completed)
        self._ephemeral_workers = workers

        def generate() -> str:
            try:
                return self._generate_memory_extraction_text(
                    prompt,
                    system_prompt=system_prompt,
                )
            finally:
                completed.set()
                self._release_requested_local_runtime_consumer_lease()

        return await asyncio.to_thread(generate)

    async def generate_memory_consolidation_async(
        self,
        prompt: str,
        *,
        system_prompt: str,
    ) -> str:
        """Use the same isolated structured path for history consolidation."""

        return await self.generate_memory_extraction_async(
            prompt,
            system_prompt=system_prompt,
        )

    def _settle_stream_usage(
        self,
        stream_usage: Dict[str, Any],
        *,
        suppress_automatic_context: bool,
    ) -> None:
        """Record and persist one plain stream request exactly once."""

        self._last_usage = dict(stream_usage)
        if stream_usage:
            self._record_request_usage(stream_usage)
        else:
            self._mark_request_usage_unknown()
        try:
            if stream_usage and not suppress_automatic_context:
                persist_usage_sync(
                    self,
                    provider="openai_compatible_local",
                    model=self.model_name,
                    usage=stream_usage,
                    request_type="chat",
                    is_streaming=True,
                )
        finally:
            self._project_request_usage_to_agent_run()

    def _stream_tool_call_fallback_allowed(
        self,
        suppress_automatic_context: bool,
    ) -> bool:
        """Return whether a tool-call-only plain stream may use the tool loop.

        Only the client's own permission-filtered registry is offered; Help
        turns and tool-disabled clients never gain provider tools here.
        """

        if suppress_automatic_context or self._is_help_isolated_turn():
            return False
        if not bool(getattr(self, "enable_tools", False)):
            return False
        try:
            return bool(filtered_registry_for_client(self, self._tool_registry))
        except Exception:
            return False

    def _stream_response(
        self,
        messages: List[Dict[str, Any]],
        temperature: Optional[float],
        max_tokens: Optional[int],
        user_input: str,
        *,
        suppress_automatic_context: Optional[bool] = None,
        event_callback: Optional[SyncStreamEmitter] = None,
    ) -> Generator[str, None, None]:
        full_response = ""
        if suppress_automatic_context is None:
            suppress_automatic_context = bool(
                getattr(get_turn_context(), "suppress_automatic_context", False)
            )
        else:
            suppress_automatic_context = bool(suppress_automatic_context)
        protect_stream_claims = _stream_request_needs_local_retrieval(user_input)
        # Keep a request-local ledger.  ``_last_usage`` is also used by
        # non-streaming calls, so reset it before starting and never persist a
        # value left over from an earlier request when stream creation fails
        # or yields no chunks.
        self._last_usage = {}
        self._last_usage_records = []
        self._last_stream_tool_call_echo_suppressed = False
        stream_usage: Dict[str, Any] = {}
        stream_usage_settled = False
        tool_loop_fallback = False
        self._last_provider_terminal_evidence = normalize_chat_terminal(
            None, transport="chat.completions.stream",
        )
        try:
            for content in self.stream_chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                suppress_automatic_context=suppress_automatic_context,
                event_callback=event_callback,
            ):
                if self._last_usage:
                    stream_usage = dict(self._last_usage)
                full_response += content
                if not protect_stream_claims:
                    yield content
            if not full_response.strip() and bool(
                getattr(self, "_last_stream_tool_call_echo_suppressed", False)
            ):
                # The model answered the tool-less plain stream with only a
                # tool call (for example LFM's special-token Pythonic call).
                # It can never execute here, so settle this stream request
                # and answer through the native tool loop, which sends the
                # permission-filtered schemas and validates/executes calls.
                if self._last_usage:
                    stream_usage = dict(self._last_usage)
                self._settle_stream_usage(
                    stream_usage,
                    suppress_automatic_context=suppress_automatic_context,
                )
                stream_usage_settled = True
                if self._stream_tool_call_fallback_allowed(
                    suppress_automatic_context
                ):
                    tool_loop_fallback = True
                    logger.warning(
                        "[OpenAICompatibleLocalClient] plain stream returned only a tool call; answering through the native tool loop"
                    )
                    full_response = str(
                        self.chat(
                            messages,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            tools_enabled=True,
                            context_budget=self._current_context_budget,
                            fallback_user_input=user_input,
                            native_tool_user_input=user_input,
                            event_callback=event_callback,
                        )
                        or ""
                    )
                else:
                    full_response = self._empty_response_fallback()
                if full_response:
                    yield full_response
        except LocalProviderNotStartedError:
            self._project_request_usage_to_agent_run()
            raise
        except AgentDialogueInputPending:
            # Streaming callers must observe the same durable worker pause as
            # non-streaming callers; do not convert it to fallback text.
            self._project_request_usage_to_agent_run()
            raise
        except PlanningInteractionTerminated:
            self._project_request_usage_to_agent_run()
            raise
        except GenerationCancelled:
            # A user stop must close the provider stream and propagate typed
            # cancellation; converting it to fallback text would emit a late
            # token/end event and make the old turn look successful.
            raise
        except GenerationInterrupted:
            raise
        except Exception as exc:
            self._mark_request_usage_unknown()
            if (
                isinstance(exc, (httpx.TransportError, APIError))
                or classify_generation_error(exc).kind in {GenerationErrorKind.CONNECTION, GenerationErrorKind.TIMEOUT}
            ):
                self._last_provider_terminal_evidence = interrupted_terminal("chat.completions.stream")
            if suppress_automatic_context:
                # Propagate the failure so the outer Help boundary emits its
                # safe, Guide-unavailable response instead of a generic local
                # model fallback.
                raise
            self._record_generation_failure(exc)
            full_response = self._get_error_response(exc)
            if _is_local_model_loading_error(exc):
                logger.warning(
                    "[OpenAICompatibleLocalClient] local model is still loading during streaming: %s",
                    exc,
                )
            elif _is_context_overflow_error(exc):
                self._reduce_context_budget_after_overflow()
                logger.warning(
                    "[OpenAICompatibleLocalClient] local model context overflow during streaming: %s",
                    exc,
                )
            else:
                logger.error(
                    "[OpenAICompatibleLocalClient] streaming failed: %s",
                    exc,
                    exc_info=True,
                )
            yield full_response
        else:
            if protect_stream_claims and not stream_usage_settled:
                full_response = guard_tool_execution_claims(full_response, [])
                if full_response:
                    yield full_response
        finally:
            if not suppress_automatic_context and not tool_loop_fallback:
                # The tool-loop fallback records its own request transcript.
                self._record_model_transcript(messages, full_response)
            if stream_usage_settled:
                self._project_request_usage_to_agent_run()
            else:
                # The final stream chunk can carry usage without content;
                # retain that observation while avoiding stale usage on an
                # empty/failed stream.  Persist exactly once for the stream.
                if self._last_usage:
                    stream_usage = dict(self._last_usage)
                self._settle_stream_usage(
                    stream_usage,
                    suppress_automatic_context=suppress_automatic_context,
                )
            if not suppress_automatic_context:
                self.history_manager.add_message("user", user_input)
                self.history_manager.add_message("assistant", full_response)

    def _record_generation_failure(self, exc: BaseException) -> None:
        """Retain bounded classified failure beside the legacy fallback."""

        failure = classify_generation_error(exc)
        detail = str(failure.technical_detail or "").strip()
        if len(detail) > 1000:
            detail = detail[:997].rstrip() + "..."
        self._last_generation_failure = dataclasses.replace(
            failure,
            technical_detail=detail,
        )
        set_current_compatible_generation_failure(self._last_generation_failure)

    def _get_error_response(self, exc: Exception) -> str:
        if isinstance(exc, LocalToolSelectionError):
            return str(exc)
        if isinstance(exc, StrataModeResolutionError):
            return STRATA_MODE_SELECTION_FAILURE_RESPONSE
        if _is_local_model_loading_error(exc):
            return LOCAL_MODEL_LOADING_RESPONSE
        if _is_context_overflow_error(exc):
            return self._get_context_overflow_fallback_response()
        if _is_connection_error(exc):
            config_error = self._managed_llama_cpp_launch_configuration_error()
            if config_error:
                try:
                    from src.service_manager import llama_cpp_runtime_requirement

                    requirement = llama_cpp_runtime_requirement(
                        self.config,
                        model=self.model_name,
                    )
                except Exception:
                    requirement = None
                requirement_hint = (
                    f" {requirement}"
                    if requirement
                    else ""
                )
                return (
                    "ローカルLLMサーバー（llama-server）を起動できませんでした。"
                    "logs/models/llama_cpp.log を確認し、"
                    "選択したprofileのruntime要件を満たすllama.cppを用意してから再試行してください。"
                    f"{requirement_hint}"
                    f"\n\n詳細:\n{config_error}"
                )
            if self._can_attempt_managed_llama_cpp_recovery() or self._uses_llama_cpp_tool_choice_transport():
                detail = str(self._last_local_server_ensure_error or "").strip()
                start_failed_response = (
                    STRATA_MODEL_SERVER_START_FAILED_RESPONSE
                    if self._managed_local_runtime == "strata"
                    else LOCAL_MODEL_SERVER_START_FAILED_RESPONSE
                )
                if detail:
                    return f"{start_failed_response}\n\n詳細: {detail}"
                return start_failed_response
            return "ローカルLLMサーバーへ接続できません。サーバーが起動しているか、base_url 設定を確認してください。"
        return self._get_fallback_response()

    def _reduce_context_budget_after_overflow(self) -> None:
        budget = self._current_context_budget or self._context_budget()
        reduced = reduced_context_window_after_overflow(budget.context_window_tokens)
        if (
            self._context_window_override_tokens is None
            or reduced < self._context_window_override_tokens
        ):
            self._context_window_override_tokens = reduced
            logger.warning(
                "[OpenAICompatibleLocalClient] Reduced local context budget after overflow: %s -> %s tokens",
                budget.context_window_tokens,
                reduced,
            )

    def _get_context_overflow_fallback_response(self) -> str:
        parts = [LOCAL_MODEL_CONTEXT_OVERFLOW_RESPONSE]
        budget = self._current_context_budget or self._context_budget()
        # Tool hints and raw result JSON are internal model context.  Expose
        # only confirmed original text/URL, or candidate titles explicitly
        # marked as not yet read; this also prevents read_args/locator/citation
        # from leaking when the model cannot complete its final answer.
        evidence_blocks = _user_visible_tool_evidence(
            getattr(self, "_last_tool_calls", None),
            max_chars=min(600, max(120, budget.context_bundle_chars // 3)),
        )
        if not evidence_blocks:
            # Project Information Docs are already a user-facing, bounded
            # context projection.  Keep that safe snapshot available when no
            # tool call produced a candidate or canonical read result.
            context_bundle = self._context_bundle_for_turn()
            project_information = str(
                getattr(context_bundle, "project_information_block", "") or ""
            ).strip()
            if project_information:
                evidence_blocks.append(
                    clip_text(project_information, budget.context_bundle_chars)
                )
        if evidence_blocks:
            parts.append("\n\n".join(evidence_blocks))
        else:
            parts.append(
                "取得済みの案件情報Docs・ツール実行結果・会話根拠はありませんでした。"
                "確認済みの原本根拠はありませんでした。"
                "メッセージを短くするか、ローカルLLMサーバーのコンテキスト設定を確認してください。"
            )
        return "\n\n".join(parts)

    def _get_fallback_response(self) -> str:
        if self.config:
            try:
                character_config = self.config.get_character_config(self.character_name)
                personality = character_config.get("personality", {})
                fallback = personality.get("fallbackReply")
                if fallback:
                    return fallback
            except Exception:
                pass
        return "すみません。ローカルLLMの呼び出しでエラーが発生しました。"

    def list_models(self) -> List[Dict[str, Any]]:
        request = urllib.request.Request(
            f"{self.base_url}/models",
            headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
        )
        with urllib.request.urlopen(request, timeout=2.0) as response:
            data = json.loads(response.read().decode("utf-8"))
        return [
            item
            for item in data.get("data", [])
            if isinstance(item, dict) and item.get("id")
        ]

    def health_check(self) -> Dict[str, Any]:
        try:
            models = self.list_models()
            return {
                "ok": True,
                "base_url": self.base_url,
                "model_count": len(models),
            }
        except Exception as exc:
            return {"ok": False, "base_url": self.base_url, "error": str(exc)}

    async def generate_response_async(
        self,
        user_input: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        image_data: Optional[Dict[str, Any]] = None,
        stream_callback: Any = None,
    ) -> str:
        from ..services.session_llm_generation import run_session_aware_generation

        return await run_session_aware_generation(
            self,
            self.config,
            user_input,
            temperature=temperature,
            max_tokens=max_tokens,
            image_data=image_data,
            stream_callback=stream_callback,
        )

    async def _generate_response_async_impl(
        self,
        user_input: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        image_data: Optional[Dict[str, Any]] = None,
        stream_callback: Any = None,
    ) -> str:
        # worker thread では実行中ループが見えないため、ここでループを束ねておく。
        # A callback-bearing async turn is the UI streaming contract.  Consume
        # the synchronous provider generator here and relay stream tokens;
        # passing the callback through while keeping ``stream=False`` would
        # silently buffer the whole LFM response until the final answer.
        bound_stream_callback = bind_stream_callback_loop(stream_callback)
        stream_requested = bound_stream_callback is not None
        owner_loop = asyncio.get_running_loop()

        def generate() -> str:
            # Worker-context only (to_thread copies the context): the owner
            # loop is never stored on the shared client, so concurrent turns
            # cannot observe each other's loop.
            owner_token = bind_tool_identity_owner_loop(owner_loop)
            try:
                return _generate_in_worker()
            finally:
                reset_tool_identity_owner_loop(owner_token)

        def _generate_in_worker() -> str:
            emitter = make_sync_stream_emitter(bound_stream_callback)

            def terminal_payload(content: str) -> dict[str, Any]:
                evidence = sanitize_terminal_evidence(
                    getattr(self, "_last_provider_terminal_evidence", None)
                )
                return {"content": content, **({"provider_terminal": evidence} if evidence else {})}
            _stream_generation_checkpoint()
            if emitter is not None:
                # Emit before entering generate_response so provider setup or
                # early callback events cannot precede stream_start.
                emitter("stream_start", {"message": "応答を生成しています"})
            result = self.generate_response(
                user_input,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=stream_requested,
                image_data=image_data,
                stream_callback=bound_stream_callback,
            )
            # A provider may return a buffered string/None (for example a
            # compatibility adapter or a test double).  Check before turning
            # that result into a final token/end pair so a stop requested
            # during provider work cannot publish a late answer.
            _stream_generation_checkpoint()
            if not stream_requested or isinstance(result, str) or result is None:
                content = str(result or "")
                if emitter is not None:
                    if content:
                        _stream_generation_checkpoint()
                        emitter("stream_token", {"content": content})
                    _stream_generation_checkpoint()
                    emitter("stream_end", terminal_payload(content))
                return content

            chunks: list[str] = []
            try:
                for chunk in result:
                    _stream_generation_checkpoint()
                    content = str(chunk or "")
                    if not content:
                        continue
                    chunks.append(content)
                    if emitter is not None:
                        emitter("stream_token", {"content": content})
                    _stream_generation_checkpoint()
                full_response = "".join(chunks)
                if emitter is not None:
                    _stream_generation_checkpoint()
                    emitter("stream_end", terminal_payload(full_response))
                return full_response
            finally:
                close = getattr(result, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        logger.debug(
                            "local provider stream close failed",
                            exc_info=True,
                        )

        return await _run_owned_local_worker(generate)

    async def generate_title_async(self, prompt: str) -> str:
        return await asyncio.to_thread(self.generate_title, prompt)

    def generate(self, prompt: str) -> str:
        return str(self.generate_response(prompt, stream=False))

    async def generate_async(self, prompt: str) -> str:
        return await self.generate_response_async(prompt)

    def clear_history(self) -> None:
        self.history_manager.clear()

    def get_history(self) -> List[Dict[str, str]]:
        return self.history_manager.get_all()

    async def cleanup(self) -> None:
        if any(not event.is_set() for event in getattr(self, "_ephemeral_workers", ())):
            # Each active request releases its own lease after transport settles.
            # Never expose the shared runtime as idle while that thread runs.
            self._local_runtime_consumer_lease_release_requested = True
            return
        self._release_managed_local_generation_lease(all=True)
        self._release_managed_local_runtime_consumer_lease(all=True)
        logger.info("[OpenAICompatibleLocalClient] cleanup complete")


def create_openai_compatible_local_client(config: Config) -> OpenAICompatibleLocalClient:
    local_config = _config_get(config, "openai_compatible_local", {}) or {}
    response_model = (
        _config_get(config, "llm_model")
        if _config_get(config, "response_model_selection_active")
        else None
    )
    model = (
        _config_get(config, "runtime.target_model")
        or response_model
        or os.getenv("OPENAI_COMPATIBLE_LOCAL_MODEL")
        or _config_get(config, "llm_model")
        or _config_get(config, "openai_compatible_local.model")
        or DEFAULT_LOCAL_MODEL
    )
    base_url = _config_get(
        config,
        "runtime.target_base_url",
    ) or openai_compatible_local_base_url(config, model=model)
    api_key = (
        _config_get(config, "runtime.target_api_key")
        or os.getenv("OPENAI_COMPATIBLE_LOCAL_API_KEY")
        or _config_get(config, "openai_compatible_local.api_key")
        or DEFAULT_LOCAL_API_KEY
    )

    return OpenAICompatibleLocalClient(
        base_url=base_url,
        model=model,
        api_key=api_key,
        config=config,
        enable_tools=bool(local_config.get("enable_tools", False)),
        enable_response_format=bool(
            local_config.get("enable_response_format", False)
        ),
        enable_extra_body=bool(local_config.get("enable_extra_body", False)),
        extra_body=local_config.get("extra_body") or {},
    )

