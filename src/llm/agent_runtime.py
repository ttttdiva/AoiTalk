"""Provider-independent agent runtime helpers.

Provider adapters should only handle transport details. Tool hints,
tool-result context shaping, and OpenAI-style tool loops live here so OpenAI,
Gemini, Ollama, SGLang, local compatible servers, and CLI adapters can share
the same agent contract.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
from types import SimpleNamespace
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Callable, Sequence

from ..tools.registry import ToolRegistry
from .aoi_vocabulary import build_aoi_vocabulary_hint
from .agentic_completion import agentic_max_rounds, response_looks_like_unfinished_work
from .generation_policy import GenerationPolicy, GenerationProfile
from .generation_policy import get_current_generation_policy
from .orchestration import build_orchestration_guidance
from .planning_policy import build_planning_system_guidance, get_current_planning_policy, get_current_planning_run_state
from .tool_packs import LOAD_TOOL_PACK_TOOL_NAME, PROJECT_TABLE_TOOL_NAMES
from .tool_policy import (
    DOCS_MUTATION_TOOL_NAMES,
    FILESYSTEM_MUTATION_TOOL_NAMES,
    FILESYSTEM_READ_TOOL_NAMES,
    FILESYSTEM_TOOL_NAMES,
    PROJECT_COMMAND_CAPABILITIES,
    PROJECT_MANAGEMENT_MUTATION_TOOL_NAMES,
    PROJECT_MANAGEMENT_READ_TOOL_NAMES,
    PROJECT_MANAGEMENT_TOOL_NAMES,
    SEARCH_TOOL_NAMES,
    command_capabilities_from_text,
    looks_like_docs_mutation_request,
    looks_like_filesystem_mutation_request,
    looks_like_managed_workspace_request,
    looks_like_media_request,
    project_progress_review_active,
    looks_like_search_request,
)
from .unified_turn_runtime import run_openai_compatible_turn_loop

logger = logging.getLogger(__name__)

DEFAULT_TOOL_HINT_CONTEXT_CHARS = 12000
PROJECT_PROGRESS_REVIEW_MAX_TOOL_ROUNDS = 120

PROJECT_PROGRESS_REFRESH_TOOL_NAMES: set[str] = {
    "organize_project_information_from_folder",
    "patch_project_information_doc",
    "attach_project_information_reference",
    "sync_wbs_tasks",
    "sync_issue_table",
    "append_record_rows",
    "update_record_row",
}

PROJECT_CONTEXT_REQUIRED_READ_TOOL_NAMES: tuple[str, ...] = (
    "list_project_information",
    "list_record_tables",
    "get_project_progress",
    "list_tasks",
    "get_project_issues",
    "get_upcoming_wbs_tasks",
    "list_calendar",
    "get_time_report",
    "summarize_project_requests",
    "render_project_diagram",
    "list_project_tasks_changed_since",
)

DIRECT_PROJECT_TOOL_HINT_NAMES: tuple[str, ...] = (
    "get_project_context",
    "list_record_tables",
    "list_project_information",
    "get_project_progress",
    "list_tasks",
    "list_calendar",
    "get_time_report",
    "get_upcoming_wbs_tasks",
    "organize_project_information_from_folder",
    "sync_wbs_tasks",
    "sync_issue_table",
    "create_record_table",
    "create_task",
    "patch_project_information_doc",
    "attach_project_information_reference",
)

DIRECT_SEARCH_TOOL_HINT_NAMES: tuple[str, ...] = (
    "web_search",
    "x_search",
    "grok_x_search",
    "knowledge_search",
    "search_past_chats",
)

DIRECT_MEMORY_TOOL_HINT_NAMES: tuple[str, ...] = ("search_past_chats",)

DIRECT_FILESYSTEM_TOOL_HINT_NAMES: tuple[str, ...] = (
    "search_files",
    "list_directory",
    "list_workspace_tree",
    "read_file",
)

@dataclass(frozen=True)
class ToolHintRule:
    tool_name: str
    detector: Callable[[str], bool]
    pack_id: str = ""


@dataclass(frozen=True)
class OpenAIToolCallRecord:
    tool: str
    arguments: dict[str, Any]
    result: str
    success: bool | None = None

    @property
    def successful(self) -> bool:
        if self.success is not None:
            return bool(self.success)
        lowered = self.result.strip().lower()
        return not (
            lowered.startswith("tool not found:")
            or lowered.startswith("error:")
            or "delegation error" in lowered
            or "requested mutation was not completed" in lowered
        )


@dataclass(frozen=True)
class OpenAIToolCallLoopResult:
    final_output: str
    tool_calls: list[OpenAIToolCallRecord]
    messages: list[dict[str, Any]] = dataclass_field(default_factory=list)
    stopped_reason: str = ""
    rounds: int = 0
    audit_tool_calls: list[OpenAIToolCallRecord] = dataclass_field(
        default_factory=list
    )


@dataclass(frozen=True)
class MissingToolExecutionClaim:
    tool_name: str
    matched_phrase: str


_VERIFIED_TOOL_EXECUTION_CLAIMS: contextvars.ContextVar[tuple[Any, ...]] = (
    contextvars.ContextVar("verified_tool_execution_claims", default=())
)


def set_verified_tool_execution_claims(
    tool_calls: Sequence[Any],
) -> contextvars.Token:
    current = _VERIFIED_TOOL_EXECUTION_CLAIMS.get()
    return _VERIFIED_TOOL_EXECUTION_CLAIMS.set((*current, *tuple(tool_calls)))


def reset_verified_tool_execution_claims(token: contextvars.Token) -> None:
    _VERIFIED_TOOL_EXECUTION_CLAIMS.reset(token)


TOOL_HINT_RULES: tuple[ToolHintRule, ...] = (
    ToolHintRule(
        tool_name="media_assistant",
        detector=looks_like_media_request,
        pack_id="media",
    ),
)

TOOL_EXECUTION_CLAIM_PATTERNS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "web_search",
        (
            "検索しました",
            "検索した結果",
            "検索したところ",
            "検索して確認",
            "検索で確認",
            "検索結果では",
            "web検索しました",
            "web検索した結果",
            "ウェブ検索しました",
            "ウェブ検索した結果",
            "調べた結果",
            "調査した結果",
            "webで確認",
            "ウェブで確認",
            "searched the web",
            "i searched",
            "looked it up online",
        ),
        (
            "検索していません",
            "検索はしていません",
            "検索できていません",
            "web検索はしていません",
            "ウェブ検索はしていません",
            "did not search",
            "didn't search",
            "have not searched",
            "no search was performed",
            "no web search was performed",
        ),
    ),
    (
        "filesystem_tools",
        (
            "ファイルを読みました",
            "ファイルを確認しました",
            "資料を読みました",
            "資料を確認しました",
            "ワークスペースを確認しました",
            "read the file",
            "checked the file",
            "inspected the workspace",
        ),
        (
            "ファイルを読んでいません",
            "ファイルは読んでいません",
            "資料を確認していません",
            "did not read the file",
            "didn't read the file",
            "have not read the file",
        ),
    ),
    (
        "project_tools",
        (
            "dbを更新しました",
            "db更新しました",
            "案件情報dbを更新",
            "プロジェクト情報dbを更新",
            "タスクを作成しました",
            "wbsを同期しました",
            "課題管理表を更新しました",
            "進捗を確認しました",
            "進捗は確認できます",
            "案件の進捗は確認できます",
            "通常タスクを根拠",
            "タスクを根拠に進捗",
            "根拠に進捗算出",
            "updated the database",
            "updated the project database",
            "created the task",
            "checked project progress",
            "project progress is",
        ),
        (
            "dbを更新していません",
            "db更新していません",
            "案件情報dbを更新していません",
            "タスクを作成していません",
            "進捗を確認していません",
            "進捗確認はしていません",
            "did not update the database",
            "didn't update the database",
            "did not create the task",
            "did not check project progress",
        ),
    ),
    (
        "utility_tools",
        (
            "現在時刻を確認しました",
            "天気を確認しました",
            "計算しました",
            "checked the current time",
            "checked the weather",
            "calculated",
        ),
        (
            "現在時刻を確認していません",
            "天気を確認していません",
            "計算していません",
            "did not check the current time",
            "did not check the weather",
        ),
    ),
)


def build_tool_hint_context_sync(
    *,
    user_input: str,
    registry: ToolRegistry,
    policy: GenerationPolicy,
    log_prefix: str = "AgentRuntime",
    max_result_chars: int = DEFAULT_TOOL_HINT_CONTEXT_CHARS,
) -> str:
    """Build compact tool hints for the parent assistant."""
    if not policy.tool_hints_enabled:
        return ""

    matched_rules, deferred_rules = _collect_tool_hint_rules(user_input, registry)

    direct_search_hint = _should_hint_direct_search_tools(user_input, registry)
    direct_memory_hint = _should_hint_direct_memory_tools(user_input, registry)
    direct_filesystem_hint = _should_hint_direct_filesystem_tools(user_input, registry)
    project_attachment_stewardship_hint = _should_hint_project_attachment_stewardship(
        user_input,
        registry,
    )
    available_filesystem_mutation_tools = tuple(
        tool_name
        for tool_name in (
            "create_workspace_directory",
            "move_workspace_item",
            "copy_workspace_item",
        )
        if tool_name in registry
    )
    direct_project_hint = _should_hint_direct_project_tools(user_input, registry)
    project_progress_review_hint = _should_hint_project_progress_review(
        user_input,
        registry,
    )
    project_organizer_hint = "organize_project_information_from_folder" in registry
    knowledge_search_hint = "knowledge_search" in registry
    aoi_vocabulary_hint = build_aoi_vocabulary_hint(
        user_input,
        inbox_search_available="inbox_search_items" in registry,
    )
    return _build_context_block(
        matched_rules,
        user_input=user_input,
        policy=policy,
        max_result_chars=max_result_chars,
        direct_search_hint=direct_search_hint,
        direct_memory_hint=direct_memory_hint,
        direct_filesystem_hint=direct_filesystem_hint,
        project_attachment_stewardship_hint=project_attachment_stewardship_hint,
        available_filesystem_mutation_tools=available_filesystem_mutation_tools,
        direct_project_hint=direct_project_hint,
        project_progress_review_hint=project_progress_review_hint,
        project_organizer_hint=project_organizer_hint,
        knowledge_search_hint=knowledge_search_hint,
        aoi_vocabulary_hint=aoi_vocabulary_hint,
        deferred_pack_rules=deferred_rules,
        project_tables_pack_hint=_project_tables_pack_hint_needed(
            user_input,
            registry,
        ),
    )


async def build_tool_hint_context_async(
    *,
    user_input: str,
    registry: ToolRegistry,
    policy: GenerationPolicy,
    log_prefix: str = "AgentRuntime",
    max_result_chars: int = DEFAULT_TOOL_HINT_CONTEXT_CHARS,
) -> str:
    """Async variant for native OpenAI-compatible clients."""
    if not policy.tool_hints_enabled:
        return ""

    matched_rules, deferred_rules = _collect_tool_hint_rules(user_input, registry)

    direct_search_hint = _should_hint_direct_search_tools(user_input, registry)
    direct_memory_hint = _should_hint_direct_memory_tools(user_input, registry)
    direct_filesystem_hint = _should_hint_direct_filesystem_tools(user_input, registry)
    project_attachment_stewardship_hint = _should_hint_project_attachment_stewardship(
        user_input,
        registry,
    )
    available_filesystem_mutation_tools = tuple(
        tool_name
        for tool_name in (
            "create_workspace_directory",
            "move_workspace_item",
            "copy_workspace_item",
        )
        if tool_name in registry
    )
    direct_project_hint = _should_hint_direct_project_tools(user_input, registry)
    project_progress_review_hint = _should_hint_project_progress_review(
        user_input,
        registry,
    )
    project_organizer_hint = "organize_project_information_from_folder" in registry
    knowledge_search_hint = "knowledge_search" in registry
    aoi_vocabulary_hint = build_aoi_vocabulary_hint(
        user_input,
        inbox_search_available="inbox_search_items" in registry,
    )
    return _build_context_block(
        matched_rules,
        user_input=user_input,
        policy=policy,
        max_result_chars=max_result_chars,
        direct_search_hint=direct_search_hint,
        direct_memory_hint=direct_memory_hint,
        direct_filesystem_hint=direct_filesystem_hint,
        project_attachment_stewardship_hint=project_attachment_stewardship_hint,
        available_filesystem_mutation_tools=available_filesystem_mutation_tools,
        direct_project_hint=direct_project_hint,
        project_progress_review_hint=project_progress_review_hint,
        project_organizer_hint=project_organizer_hint,
        knowledge_search_hint=knowledge_search_hint,
        aoi_vocabulary_hint=aoi_vocabulary_hint,
        deferred_pack_rules=deferred_rules,
        project_tables_pack_hint=_project_tables_pack_hint_needed(
            user_input,
            registry,
        ),
    )


def compose_tool_hint_user_message(
    user_input: str,
    tool_hint_context: str,
) -> str:
    """Build the user message seen by the parent LLM with tool hints."""
    if not tool_hint_context:
        return user_input
    return f"{tool_hint_context}\n\nCurrent user request:\n{user_input}"


def project_context_required_read_tool_names(
    registry: ToolRegistry | None = None,
) -> tuple[str, ...]:
    """Return read tools that satisfy the project-context grounding requirement."""
    names = [
        name
        for name in PROJECT_CONTEXT_REQUIRED_READ_TOOL_NAMES
        if name in PROJECT_MANAGEMENT_READ_TOOL_NAMES
    ]
    if registry is None:
        return tuple(names)
    return tuple(name for name in names if name in registry)


def project_context_read_satisfied(tool_results: Sequence[Any]) -> bool:
    required_read_tools = set(project_context_required_read_tool_names())
    return any(
        _tool_result_name(result) in required_read_tools
        and _tool_result_successful(result)
        for result in tool_results
    )


def project_context_read_final_response_check(
    *,
    required: bool,
) -> Callable[[str, Sequence[Any], int], str | None] | None:
    if not required:
        return None

    def _check(
        final_output: str,
        tool_results: Sequence[Any],
        round_index: int,
    ) -> str | None:
        return _project_context_read_continuation_prompt(
            tool_results=tool_results,
            round_index=round_index,
        )

    return _check


def combined_final_response_check(
    *,
    user_input: str | None,
    require_project_context_read: bool,
) -> Callable[[str, Sequence[Any], int], str | None] | None:
    checks = [
        check
        for check in (
            project_context_read_final_response_check(
                required=require_project_context_read,
            ),
            _project_progress_review_final_response_check(user_input),
            _unfinished_work_final_response_check(user_input),
        )
        if check is not None
    ]
    if not checks:
        return None

    def _check(
        final_output: str,
        tool_results: Sequence[Any],
        round_index: int,
    ) -> str | None:
        for check in checks:
            continuation = check(final_output, tool_results, round_index)
            if continuation:
                return continuation
        return None

    return _check


def managed_workspace_evidence_missing(
    user_input: str | None,
    tool_results: Sequence[Any],
) -> tuple[str, ...]:
    """Return the high-level steps still missing for a managed workspace turn."""

    text = str(user_input or "")
    if not looks_like_managed_workspace_request(text):
        return ()
    successful = {
        _tool_result_name(result)
        for result in tool_results
        if _tool_result_successful(result)
    }
    missing: list[str] = []
    if looks_like_filesystem_mutation_request(text):
        if not successful.intersection({"list_workspace_tree", "list_directory"}):
            missing.append("inspect the target Project tree once with `list_workspace_tree`")
        if not successful.intersection(
            {"copy_workspace_item", "move_workspace_item", "docs_place_workspace_file"}
        ):
            missing.append(
                "place the file with `copy_workspace_item`, `move_workspace_item`, "
                "or `docs_place_workspace_file`"
            )
    if looks_like_docs_mutation_request(text) and not successful.intersection(
        {"docs_attach_workspace_file", "docs_place_workspace_file"}
    ):
        missing.append(
            "add the idempotent Docs link with `docs_attach_workspace_file`, or "
            "prefer `docs_place_workspace_file` to place and attach in one call"
        )
    return tuple(missing)


def _unfinished_work_final_response_check(
    user_input: str | None,
) -> Callable[[str, Sequence[Any], int], str | None] | None:
    if not user_input:
        return None

    def _check(
        final_output: str,
        tool_results: Sequence[Any],
        round_index: int,
    ) -> str | None:
        if not response_looks_like_unfinished_work(user_input, final_output):
            return None
        return (
            "Do not return a plan-only final answer. Perform the remaining work "
            "now with the AoiTalk tools already provided, then report the "
            "confirmed result. Do not re-run successful tool calls."
        )

    return _check


def _project_context_read_continuation_prompt(
    *,
    tool_results: Sequence[Any],
    round_index: int,
) -> str | None:
    if project_context_read_satisfied(tool_results):
        return None
    tools = ", ".join(
        f"`{name}`" for name in project_context_required_read_tool_names()
    )
    return (
        "Do not produce the final answer yet. Project context is enabled, but "
        "no successful project DB read tool result is available. Continue with "
        f"tool calls now: call at least one of {tools} first. This requirement "
        "is read-only grounding; after that read succeeds, decide normally "
        "whether more reads or any project DB mutation tools are needed for "
        "the user's request."
    )


def _project_progress_review_final_response_check(
    user_input: str | None,
) -> Callable[[str, Sequence[Any], int], str | None] | None:
    if not user_input or not project_progress_review_active(user_input):
        return None

    def _check(
        final_output: str,
        tool_results: Sequence[Any],
        round_index: int,
    ) -> str | None:
        return _project_progress_review_continuation_prompt(
            final_output=final_output,
            tool_results=tool_results,
            round_index=round_index,
        )

    return _check


def _project_progress_review_continuation_prompt(
    *,
    final_output: str,
    tool_results: Sequence[Any],
    round_index: int,
) -> str | None:
    successful_tools = [
        _tool_result_name(result)
        for result in tool_results
        if _tool_result_successful(result)
    ]
    progress_indices = [
        index
        for index, result in enumerate(tool_results)
        if _tool_result_name(result) == "get_project_progress"
        and _tool_result_successful(result)
    ]
    if not progress_indices:
        return (
            "Do not produce the final answer yet. This is project progress "
            "review mode, and no successful `get_project_progress` result is "
            "available. Continue with tool calls now: call `get_project_progress` "
            "for the selected/current project first, then inspect DB, record "
            "tables, tasks, project files, or web sources as needed before "
            "answering."
        )

    last_progress_index = progress_indices[-1]
    mutation_indices = [
        index
        for index, result in enumerate(tool_results)
        if _tool_result_name(result) in PROJECT_PROGRESS_REFRESH_TOOL_NAMES
        and _tool_result_successful(result)
    ]
    if mutation_indices and mutation_indices[-1] > last_progress_index:
        return (
            "Do not produce the final answer yet. Project information was "
            "updated after the latest `get_project_progress` check. Continue "
            "with tool calls and run `get_project_progress` again so the final "
            "answer is based on the updated DB state."
        )

    progress_payload = _latest_project_progress_payload(tool_results)
    if (
        progress_payload
        and progress_payload.get("can_assess_progress") is False
        and "organize_project_information_from_folder" not in successful_tools
    ):
        if get_current_generation_policy().profile == GenerationProfile.REVIEW:
            return (
                "Do not produce the final answer yet. `get_project_progress` says "
                "stored progress evidence is insufficient. Continue with read-only "
                "tool calls: inspect `list_project_information`, "
                "`list_record_tables`, tasks, relevant project files, and public "
                "web sources when external current facts are required. Do not "
                "modify project information in review mode."
            )
        return (
            "Do not produce the final answer yet. `get_project_progress` says "
            "stored progress evidence is insufficient. Continue with tool calls: "
            "inspect `list_project_information`, `list_record_tables`, tasks, "
            "and relevant project filer files. If the selected project filer may "
            "contain newer source documents, call "
            "`organize_project_information_from_folder` with `folder_path=\"\"` "
            "and `apply=true`, then run `get_project_progress` again. Use web "
            "search only if external current facts are required."
        )

    return None


def _latest_project_progress_payload(tool_results: Sequence[Any]) -> dict[str, Any]:
    for result in reversed(tool_results):
        if _tool_result_name(result) != "get_project_progress":
            continue
        payload = _json_object_from_tool_output(_tool_result_output(result))
        if payload:
            return payload
    return {}


def _json_object_from_tool_output(output: str) -> dict[str, Any]:
    text = str(output or "").strip()
    if not text:
        return {}
    for candidate in (text, text[text.find("{") : text.rfind("}") + 1]):
        candidate = candidate.strip()
        if not candidate.startswith("{") or not candidate.endswith("}"):
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _tool_result_name(result: Any) -> str:
    call = getattr(result, "call", None)
    if call is not None:
        return str(getattr(call, "tool", "") or "")
    return str(getattr(result, "tool", "") or "")


def _tool_result_output(result: Any) -> str:
    if hasattr(result, "model_output"):
        return str(getattr(result, "model_output") or "")
    return str(getattr(result, "result", "") or getattr(result, "output", "") or "")


def _tool_result_successful(result: Any) -> bool:
    if hasattr(result, "success") and not bool(getattr(result, "success")):
        return False
    if hasattr(result, "successful") and not bool(getattr(result, "successful")):
        return False
    raw_output = _tool_result_output(result).strip()
    if raw_output.startswith("{") and raw_output.endswith("}"):
        try:
            payload = json.loads(raw_output)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and payload.get("success") is False:
            return False
    output = raw_output.lower()
    return not (
        output.startswith("error:")
        or output.startswith("tool not found:")
        or output.startswith("tool execution error:")
    )


def _openai_tool_call_record_from_unified(tool_result: Any) -> OpenAIToolCallRecord:
    return OpenAIToolCallRecord(
        tool=tool_result.call.tool,
        arguments=dict(tool_result.call.arguments),
        result=tool_result.model_output,
        success=bool(tool_result.success),
    )


def run_openai_tool_call_loop(
    *,
    initial_messages: list[dict[str, Any]],
    assistant_message: Any,
    api_kwargs: dict[str, Any],
    registry: ToolRegistry,
    create_completion: Callable[[dict[str, Any]], Any],
    log_prefix: str = "AgentRuntime",
    max_rounds: int = 5,
    return_result: bool = False,
    max_tool_result_chars: int | None = None,
    message_content: Callable[[Any], str] | None = None,
    config: Any | None = None,
    user_input: str | None = None,
    enforce_tool_policy: bool = True,
    require_project_context_read: bool = False,
    skip_final_response_check_on_empty: bool = False,
    event_callback: Callable[[str, dict[str, Any]], Any] | None = None,
    restore_tool_arguments: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> str | OpenAIToolCallLoopResult:
    """Execute OpenAI-compatible tool calls and re-prompt until text output."""
    effective_user_input = user_input
    if not effective_user_input:
        for message in reversed(initial_messages):
            if message.get("role") == "user":
                effective_user_input = str(message.get("content") or "")
                break
    effective_max_rounds = max_rounds
    current_policy = get_current_generation_policy()
    effective_max_rounds = max(
        effective_max_rounds,
        agentic_max_rounds(
            SimpleNamespace(config=config, generation_policy=current_policy),
            effective_user_input,
        ),
    )
    if effective_user_input and project_progress_review_active(effective_user_input):
        effective_max_rounds = max(
            effective_max_rounds,
            PROJECT_PROGRESS_REVIEW_MAX_TOOL_ROUNDS,
        )
    result = run_openai_compatible_turn_loop(
        initial_messages=initial_messages,
        assistant_message=assistant_message,
        api_kwargs=api_kwargs,
        registry=registry,
        create_completion=create_completion,
        log_prefix=log_prefix,
        max_rounds=effective_max_rounds,
        max_tool_result_chars=max_tool_result_chars,
        message_content=message_content,
        config=config,
        user_input=effective_user_input,
        enforce_tool_policy=enforce_tool_policy,
        final_response_check=combined_final_response_check(
            user_input=effective_user_input,
            require_project_context_read=require_project_context_read,
        ),
        skip_final_response_check_on_empty=skip_final_response_check_on_empty,
        event_callback=event_callback,
        restore_tool_arguments=restore_tool_arguments,
    )
    if not return_result:
        return result.final_output
    return OpenAIToolCallLoopResult(
        final_output=result.final_output,
        tool_calls=[
            _openai_tool_call_record_from_unified(tool_result)
            for tool_result in result.tool_results
        ],
        messages=[dict(message) for message in result.messages],
        stopped_reason=result.stopped_reason,
        rounds=result.rounds,
        audit_tool_calls=[
            _openai_tool_call_record_from_unified(tool_result)
            for tool_result in result.audit_tool_results
        ],
    )


def _message_content(message: Any, extractor: Callable[[Any], str] | None) -> str:
    if extractor is not None:
        return str(extractor(message) or "")
    return str(getattr(message, "content", None) or "")


def _collect_tool_hint_rules(
    user_input: str,
    registry: ToolRegistry,
) -> tuple[list[ToolHintRule], list[ToolHintRule]]:
    """公開済みルールと、pack 未ロードで案内だけ出すルールに分ける。"""
    matched: list[ToolHintRule] = []
    deferred: list[ToolHintRule] = []
    load_available = LOAD_TOOL_PACK_TOOL_NAME in registry
    for rule in TOOL_HINT_RULES:
        if not rule.detector(user_input):
            continue
        if rule.tool_name in registry:
            matched.append(rule)
        elif load_available and rule.pack_id:
            deferred.append(rule)
    return matched, deferred


def _project_tables_pack_hint_needed(
    user_input: str,
    registry: ToolRegistry,
) -> bool:
    if LOAD_TOOL_PACK_TOOL_NAME not in registry:
        return False
    # A table/WBS pack is a command capability concern.  Ordinary mentions of
    # "WBS", "DB", or "タスク" must remain available for the model's own
    # selection rather than injecting a pack hint.
    if not command_capabilities_from_text(user_input) & {
        "project_db_update",
        "wbs_sync",
    }:
        return False
    return not any(name in registry for name in PROJECT_TABLE_TOOL_NAMES)


def _should_hint_direct_search_tools(user_input: str, registry: ToolRegistry) -> bool:
    return looks_like_search_request(user_input) and any(
        tool_name in registry for tool_name in DIRECT_SEARCH_TOOL_HINT_NAMES
    )


def _should_hint_direct_memory_tools(user_input: str, registry: ToolRegistry) -> bool:
    # There is no natural-language memory capability.  Keep the normal tool
    # catalog untouched and let the model choose `search_past_chats` when the
    # request actually needs it.
    return False


def _should_hint_direct_filesystem_tools(user_input: str, registry: ToolRegistry) -> bool:
    # Files/Docs words alone do not establish that a filesystem tool is needed.
    # Verified Project attachments use the dedicated stewardship hint below.
    return False


def _should_hint_project_attachment_stewardship(
    user_input: str,
    registry: ToolRegistry,
) -> bool:
    """Use only task-local server metadata for attachment stewardship."""
    try:
        from ..services.turn_context import get_turn_context

        if not get_turn_context().verified_project_attachment:
            return False
    except Exception:
        return False
    return any(
        tool_name in registry
        for tool_name in (
            *DIRECT_FILESYSTEM_TOOL_HINT_NAMES,
            *FILESYSTEM_MUTATION_TOOL_NAMES,
        )
    )


def _should_hint_direct_project_tools(user_input: str, registry: ToolRegistry) -> bool:
    capabilities = command_capabilities_from_text(user_input)
    return bool(capabilities & PROJECT_COMMAND_CAPABILITIES) and any(
        tool_name in registry for tool_name in DIRECT_PROJECT_TOOL_HINT_NAMES
    )


def _should_hint_project_progress_review(user_input: str, registry: ToolRegistry) -> bool:
    return project_progress_review_active(user_input) and any(
        tool_name in registry for tool_name in DIRECT_PROJECT_TOOL_HINT_NAMES
    )


def _build_context_block(
    rules: Sequence[ToolHintRule],
    *,
    user_input: str,
    policy: GenerationPolicy,
    max_result_chars: int = DEFAULT_TOOL_HINT_CONTEXT_CHARS,
    direct_search_hint: bool = False,
    direct_memory_hint: bool = False,
    direct_filesystem_hint: bool = False,
    project_attachment_stewardship_hint: bool = False,
    available_filesystem_mutation_tools: Sequence[str] = (),
    direct_project_hint: bool = False,
    project_progress_review_hint: bool = False,
    project_organizer_hint: bool = False,
    knowledge_search_hint: bool = False,
    aoi_vocabulary_hint: str = "",
    deferred_pack_rules: Sequence[ToolHintRule] = (),
    project_tables_pack_hint: bool = False,
) -> str:
    autonomous_execution_guidance = (
        "## Autonomous Tool Execution\n"
        "- Use the available tools without asking the user for execution permission. "
        "Ask only when required information or a consequential user choice is missing."
        if policy.permission_policy.value == "auto_approve"
        else ""
    )
    if (
        not rules
        and not deferred_pack_rules
        and not direct_search_hint
        and not direct_memory_hint
        and not direct_filesystem_hint
        and not project_attachment_stewardship_hint
        and not direct_project_hint
        and not project_progress_review_hint
        and not aoi_vocabulary_hint
    ):
        return autonomous_execution_guidance
    tool_lines = [f"- Consider `{rule.tool_name}`." for rule in rules]
    for rule in deferred_pack_rules:
        tool_lines.append(
            f"- `{rule.tool_name}` はまだロードされていません。"
            f'`load_tool_pack` に pack="{rule.pack_id}" を渡してロードしてから使ってください。'
        )
    if direct_search_hint:
        if knowledge_search_hint:
            tool_lines.append(
                "- 公開Webや最新情報は `web_search`、X/Twitterはまず `x_search`（Yahooリアルタイム検索）、"
                "不足する場合だけ `grok_x_search`、"
                "Knowledge Sourceは `knowledge_search` を使って確認してください。"
            )
        else:
            tool_lines.append(
                "- 公開Webや最新情報は `web_search`、"
                "X/Twitterはまず `x_search`（Yahooリアルタイム検索）、不足する場合だけ "
                "`grok_x_search` を使って確認してください。"
            )
    if direct_memory_hint:
        tool_lines.append(
            "- ユーザーの好み・名前・過去の決定・以前の作業内容など、現在の会話に無い文脈が"
            "必要になったら `search_past_chats` で過去会話を検索してください。"
            "自動で添えられた過去会話の抜粋で足りない場合も `search_past_chats` で掘り下げてください。"
        )
    if direct_filesystem_hint:
        tool_lines.append(
            "- ワークスペースやファイル確認は `search_files`、`list_directory`、"
            "`list_workspace_tree`、`read_file` を使ってください。配置先を選ぶ時は"
            "同じ探索を繰り返さず、`list_workspace_tree` で既存構成を1回確認してください。"
        )
    if project_attachment_stewardship_hint:
        if available_filesystem_mutation_tools:
            mutation_step = (
                "、".join(
                    f"`{tool_name}`"
                    for tool_name in available_filesystem_mutation_tools
                )
                + "で必要最小限の整理を行い、最後に一覧で確認してください。"
            )
        else:
            mutation_step = (
                "このターンではworkspace変更ツールが利用できないため、"
                "分類と配置候補の確認までに留めてください。"
            )
        tool_lines.extend(
            [
                "- Project workspace内の添付を検出しました。これは会話専用の一時置き場ではなく、"
                "継続管理するProject資産の候補です。テンプレート・参照資料・ソース・成果物などは"
                "継続資産、今回だけの入力や一時出力は一時資料として分類してください。",
                "- 継続資産なら、まず `list_workspace_tree` でProject workspaceの"
                "既存構成を確認し、対応するフォルダを再利用してください。対応先がなければ"
                f"{mutation_step}",
                "- 一時資料は `attachments` に残し、無関係な既存ファイルは移動しないでください。"
                "分類に必要な内容は `read_file` で確認し、名前や拡張子だけで推測しないでください。",
            ]
        )
    if direct_project_hint:
        tool_lines.append(
            "- 案件情報は `list_project_information`、台帳は `list_record_tables`、"
            "タスク一覧/未完了/期限は `list_tasks`、予定は `list_calendar`、"
            "作業時間は `get_time_report` を使ってください。"
        )
        tool_lines.append(
            "- 進捗確認や状況確認は `get_project_progress` を起点にし、"
            "必要なら案件情報、台帳、タスク、予定、関連ファイルも確認してください。"
            "進捗は期間内の活動量ではなく案件目標への到達度です。"
        )
        tool_lines.append(
            "- タスク追加依頼は聞き返さず `create_task` を呼び、内容から簡潔なタイトルを作り、"
            "日時は due_date / start_at / end_at、詳細は description に入れてください。"
        )
        tool_lines.append(
            "- 新規タスクの作成前に、選択中Projectの既存タスクを `list_tasks`（必要なら search 付き）で"
            "確認し、parent_task_id 階層を尊重してください。明確な既存の関連root/containerがあれば"
            "そのsubtaskにし、なければ同一目的は1つのrootと実行可能なsubtasksにまとめ、独立成果だけを"
            "別rootにしてください。タイトルの曖昧な類似だけで統合せず、横断的な関連・依存をcontainmentと"
            "混同したり、重複containerを乱立させたりしないでください。"
        )
        tool_lines.append(
            "- 案件情報Docsは正本です。書く前に `list_project_information` で読み、"
            "見出し構造を保ったまま `patch_project_information_doc` で該当箇所だけを更新し、"
            "change_summary と source_refs_json を残してください。"
        )
    if project_tables_pack_hint:
        tool_lines.append(
            "- 台帳 / WBS / 課題管理表の作成・更新・同期ツールは未ロードです。"
            '`load_tool_pack` に pack="project_tables" を渡してロードしてから使ってください。'
        )
    if project_progress_review_hint:
        tool_lines.append(
            "- 進捗レビューでは `get_project_progress` から始め、回答に必要な根拠が揃うまで追加確認してください。"
        )
        evidence_line = (
            "- 根拠が不足、古い、または矛盾している場合は `list_project_information`、"
            "`list_record_tables`、タスク、予定、関連ファイルを確認してください。"
        )
        if project_organizer_hint:
            evidence_line += (
                "選択中プロジェクトの資料が新しい根拠になり得る場合は "
                "`organize_project_information_from_folder` を使ってください。"
            )
        tool_lines.append(evidence_line)
        if project_organizer_hint:
            tool_lines.append(
                "- 案件情報Docsや台帳を更新した場合は、最終回答前に "
                "`get_project_progress` を再実行してください。"
            )
    block = (
        "\n".join(["## Tool Hints", *tool_lines]).strip()
        if tool_lines
        else ""
    )
    if aoi_vocabulary_hint:
        block = (
            f"{block}\n\n{aoi_vocabulary_hint}"
            if block
            else aoi_vocabulary_hint
        )
    if autonomous_execution_guidance:
        block = (
            f"{block}\n\n{autonomous_execution_guidance}"
            if block
            else autonomous_execution_guidance
        )
    matched_tool_names = [rule.tool_name for rule in rules]
    if direct_search_hint:
        matched_tool_names.append("search_tools")
    if direct_memory_hint:
        matched_tool_names.append("memory_tools")
    if direct_filesystem_hint:
        matched_tool_names.append("filesystem_tools")
    if direct_project_hint:
        matched_tool_names.append("project_tools")
    if project_progress_review_hint:
        matched_tool_names.append("project_progress_review")
    guidance = build_orchestration_guidance(
        user_input=user_input,
        matched_tool_names=matched_tool_names,
        policy=policy,
    )
    if guidance:
        block = f"{block}\n\n{guidance}" if block else guidance
    planning_state = get_current_planning_run_state()
    planning_guidance = build_planning_system_guidance(
        planning_policy=get_current_planning_policy(),
        generation_policy=policy,
        approved_plan=planning_state.plan if planning_state else None,
    )
    if planning_guidance:
        block = f"{block}\n\n{planning_guidance}" if block else planning_guidance
    return _clip_text(block, max_result_chars)


def find_missing_tool_execution_claims(
    response_text: str,
    tool_calls: Sequence[Any],
) -> list[MissingToolExecutionClaim]:
    text = str(response_text or "")
    if not text.strip():
        return []
    available_tool_calls = (
        *_VERIFIED_TOOL_EXECUTION_CLAIMS.get(),
        *tuple(tool_calls),
    )
    missing: list[MissingToolExecutionClaim] = []
    for tool_name, positive_patterns, negative_patterns in TOOL_EXECUTION_CLAIM_PATTERNS:
        matched_phrase = _matched_execution_claim(
            text,
            positive_patterns=positive_patterns,
            negative_patterns=negative_patterns,
        )
        if not matched_phrase:
            continue
        if _has_successful_tool_call(available_tool_calls, tool_name):
            continue
        missing.append(
            MissingToolExecutionClaim(
                tool_name=tool_name,
                matched_phrase=matched_phrase,
            )
        )
    return missing


def guard_tool_execution_claims(
    response_text: str,
    tool_calls: Sequence[Any],
) -> str:
    missing = find_missing_tool_execution_claims(response_text, tool_calls)
    if not missing:
        return response_text
    tool_names = ", ".join(f"`{item.tool_name}`" for item in missing)
    logger.warning(
        "Blocked response with unverified tool execution claim: %s",
        ", ".join(f"{item.tool_name}:{item.matched_phrase}" for item in missing),
    )
    return (
        "ツール実行の検証に失敗しました。"
        f"回答は {tool_names} を実行済みとして述べていますが、"
        "対応する成功した tool result が記録されていません。"
        "検索・読取・更新などの実行済み主張は、該当ツールの結果がある場合だけ行えます。"
    )


def _matched_execution_claim(
    text: str,
    *,
    positive_patterns: Sequence[str],
    negative_patterns: Sequence[str],
) -> str:
    lowered = text.casefold()
    if any(pattern.casefold() in lowered for pattern in negative_patterns):
        return ""
    for pattern in positive_patterns:
        if pattern.casefold() in lowered:
            return pattern
    return ""


def _has_successful_tool_call(tool_calls: Sequence[Any], tool_name: str) -> bool:
    aliases = _tool_execution_aliases(tool_name)
    return any(
        _tool_call_name(call) in aliases and _tool_call_successful(call)
        for call in tool_calls
    )


def _tool_execution_aliases(tool_name: str) -> set[str]:
    if tool_name == "web_search":
        return SEARCH_TOOL_NAMES
    if tool_name == "filesystem_tools":
        return FILESYSTEM_TOOL_NAMES
    if tool_name == "project_tools":
        return PROJECT_MANAGEMENT_TOOL_NAMES
    if tool_name == "utility_tools":
        return {"get_current_time", "get_weather_info", "calculate"}
    return {tool_name}


def _tool_call_name(call: Any) -> str:
    if isinstance(call, dict):
        return str(call.get("tool") or call.get("name") or "")
    return str(getattr(call, "tool", "") or getattr(call, "name", "") or "")


def _tool_call_successful(call: Any) -> bool:
    if isinstance(call, dict):
        successful = call.get("successful")
        if isinstance(successful, bool):
            return successful
        result = str(call.get("result") or "")
    else:
        successful = getattr(call, "successful", None)
        if isinstance(successful, bool):
            return successful
        result = str(getattr(call, "result", "") or "")
    lowered = result.strip().lower()
    return not (
        lowered.startswith("tool not found:")
        or lowered.startswith("error:")
        or lowered.startswith("tool execution error:")
        or "delegation error" in lowered
        or "requested mutation was not completed" in lowered
    )


def _clip_text(text: str, max_chars: int | None) -> str:
    if not max_chars or max_chars <= 0:
        return text
    if len(text) <= max_chars:
        return text
    suffix = "\n... (truncated to fit the model context budget)"
    keep = max(0, max_chars - len(suffix))
    return text[:keep].rstrip() + suffix
