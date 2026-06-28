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
from types import SimpleNamespace
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from ..tools.registry import ToolRegistry
from .agentic_completion import agentic_max_rounds
from .generation_policy import GenerationPolicy
from .generation_policy import get_current_generation_policy
from .orchestration import build_orchestration_guidance
from .tool_policy import (
    FILESYSTEM_READ_TOOL_NAMES,
    FILESYSTEM_TOOL_NAMES,
    PROJECT_MANAGEMENT_MUTATION_TOOL_NAMES,
    PROJECT_MANAGEMENT_READ_TOOL_NAMES,
    PROJECT_MANAGEMENT_TOOL_NAMES,
    SEARCH_TOOL_NAMES,
    looks_like_filesystem_request,
    looks_like_media_request,
    looks_like_project_management_request,
    project_progress_review_active,
    looks_like_memory_request,
    looks_like_search_request,
    looks_like_utility_request,
)
from .unified_turn_runtime import run_openai_compatible_turn_loop

logger = logging.getLogger(__name__)

DEFAULT_TOOL_HINT_CONTEXT_CHARS = 12000
PROJECT_PROGRESS_REVIEW_MAX_TOOL_ROUNDS = 120

PROJECT_PROGRESS_REFRESH_TOOL_NAMES: set[str] = {
    "organize_project_information_from_folder",
    "sync_wbs_tasks",
    "sync_issue_table",
    "append_record_rows",
    "update_record_row",
    "upsert_project_fact",
    "upsert_project_info_category",
    "register_project_document",
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
    "upsert_project_fact",
)

DIRECT_SEARCH_TOOL_HINT_NAMES: tuple[str, ...] = (
    "web_search",
    "grok_x_search",
    "knowledge_search",
    "search_memory",
)

DIRECT_MEMORY_TOOL_HINT_NAMES: tuple[str, ...] = ("search_memory",)

DIRECT_FILESYSTEM_TOOL_HINT_NAMES: tuple[str, ...] = (
    "find_workspace_items",
    "inspect_workspace_tree",
    "read_workspace_file",
    "view_file",
    "search_files",
    "list_directory",
)


@dataclass(frozen=True)
class ToolHintRule:
    tool_name: str
    detector: Callable[[str], bool]


@dataclass(frozen=True)
class OpenAIToolCallRecord:
    tool: str
    arguments: dict[str, Any]
    result: str

    @property
    def successful(self) -> bool:
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
        tool_name="utility_assistant",
        detector=looks_like_utility_request,
    ),
    ToolHintRule(
        tool_name="media_assistant",
        detector=looks_like_media_request,
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
        "utility_assistant",
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

    matched_rules: list[ToolHintRule] = []
    for rule in TOOL_HINT_RULES:
        if not _should_run_tool_hint_rule(rule, user_input, registry):
            continue
        matched_rules.append(rule)

    direct_search_hint = _should_hint_direct_search_tools(user_input, registry)
    direct_memory_hint = _should_hint_direct_memory_tools(user_input, registry)
    direct_filesystem_hint = _should_hint_direct_filesystem_tools(user_input, registry)
    direct_project_hint = _should_hint_direct_project_tools(user_input, registry)
    project_progress_review_hint = _should_hint_project_progress_review(
        user_input,
        registry,
    )
    return _build_context_block(
        matched_rules,
        user_input=user_input,
        policy=policy,
        max_result_chars=max_result_chars,
        direct_search_hint=direct_search_hint,
        direct_memory_hint=direct_memory_hint,
        direct_filesystem_hint=direct_filesystem_hint,
        direct_project_hint=direct_project_hint,
        project_progress_review_hint=project_progress_review_hint,
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

    matched_rules: list[ToolHintRule] = []
    for rule in TOOL_HINT_RULES:
        if not _should_run_tool_hint_rule(rule, user_input, registry):
            continue
        matched_rules.append(rule)

    direct_search_hint = _should_hint_direct_search_tools(user_input, registry)
    direct_memory_hint = _should_hint_direct_memory_tools(user_input, registry)
    direct_filesystem_hint = _should_hint_direct_filesystem_tools(user_input, registry)
    direct_project_hint = _should_hint_direct_project_tools(user_input, registry)
    project_progress_review_hint = _should_hint_project_progress_review(
        user_input,
        registry,
    )
    return _build_context_block(
        matched_rules,
        user_input=user_input,
        policy=policy,
        max_result_chars=max_result_chars,
        direct_search_hint=direct_search_hint,
        direct_memory_hint=direct_memory_hint,
        direct_filesystem_hint=direct_filesystem_hint,
        direct_project_hint=direct_project_hint,
        project_progress_review_hint=project_progress_review_hint,
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


def _combined_final_response_check(
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
    if hasattr(result, "success"):
        return bool(getattr(result, "success"))
    if hasattr(result, "successful"):
        return bool(getattr(result, "successful"))
    output = _tool_result_output(result).strip().lower()
    return not (
        output.startswith("error:")
        or output.startswith("tool not found:")
        or output.startswith("tool execution error:")
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
        final_response_check=_combined_final_response_check(
            user_input=effective_user_input,
            require_project_context_read=require_project_context_read,
        ),
    )
    if not return_result:
        return result.final_output
    return OpenAIToolCallLoopResult(
        final_output=result.final_output,
        tool_calls=[
            OpenAIToolCallRecord(
                tool=tool_result.call.tool,
                arguments=dict(tool_result.call.arguments),
                result=tool_result.model_output,
            )
            for tool_result in result.tool_results
        ],
    )


def _message_content(message: Any, extractor: Callable[[Any], str] | None) -> str:
    if extractor is not None:
        return str(extractor(message) or "")
    return str(getattr(message, "content", None) or "")


def _should_run_tool_hint_rule(
    rule: ToolHintRule,
    user_input: str,
    registry: ToolRegistry,
) -> bool:
    return rule.tool_name in registry and rule.detector(user_input)


def _should_hint_direct_search_tools(user_input: str, registry: ToolRegistry) -> bool:
    return looks_like_search_request(user_input) and any(
        tool_name in registry for tool_name in DIRECT_SEARCH_TOOL_HINT_NAMES
    )


def _should_hint_direct_memory_tools(user_input: str, registry: ToolRegistry) -> bool:
    return looks_like_memory_request(user_input) and any(
        tool_name in registry for tool_name in DIRECT_MEMORY_TOOL_HINT_NAMES
    )


def _should_hint_direct_filesystem_tools(user_input: str, registry: ToolRegistry) -> bool:
    return looks_like_filesystem_request(user_input) and any(
        tool_name in registry for tool_name in DIRECT_FILESYSTEM_TOOL_HINT_NAMES
    )


def _should_hint_direct_project_tools(user_input: str, registry: ToolRegistry) -> bool:
    return looks_like_project_management_request(user_input) and any(
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
    direct_project_hint: bool = False,
    project_progress_review_hint: bool = False,
) -> str:
    if (
        not rules
        and not direct_search_hint
        and not direct_memory_hint
        and not direct_filesystem_hint
        and not direct_project_hint
        and not project_progress_review_hint
    ):
        return ""
    tool_lines = [f"- Consider `{rule.tool_name}`." for rule in rules]
    if direct_search_hint:
        tool_lines.append(
            "- 公開Webや最新情報は `web_search`、X/Twitterは `grok_x_search`、"
            "Knowledge Sourceは `knowledge_search`、過去会話は `search_memory` を使って確認してください。"
        )
    if direct_memory_hint:
        tool_lines.append(
            "- 過去会話、以前話した内容、ユーザーが覚えているか確認している内容は "
            "`search_memory` を使って確認してください。"
        )
    if direct_filesystem_hint:
        tool_lines.append(
            "- ワークスペースやファイル確認は `find_workspace_items`、"
            "`inspect_workspace_tree`、`read_workspace_file`、`view_file`、"
            "`search_files`、`list_directory` を使って、必要なファイルまで読んでください。"
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
        )
    if project_progress_review_hint:
        tool_lines.append(
            "- 進捗レビューでは `get_project_progress` から始め、回答に必要な根拠が揃うまで追加確認してください。"
        )
        tool_lines.append(
            "- 根拠が不足、古い、または矛盾している場合は `list_project_information`、"
            "`list_record_tables`、タスク、予定、関連ファイルを確認してください。"
            "選択中プロジェクトの資料が新しい根拠になり得る場合は "
            "`organize_project_information_from_folder` を使ってください。"
        )
        tool_lines.append(
            "- 案件DBや台帳を更新した場合は、最終回答前に `get_project_progress` を再実行してください。"
        )
    block = "\n".join(["## Tool Hints", *tool_lines]).strip()
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
        block = f"{block}\n\n{guidance}"
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
